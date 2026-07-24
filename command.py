import json
import os
import datetime
import math

from sessions import (
    session, save_session, fresh_town_map, neighbor_session, is_friend,
    is_enemy_camp_marker, mark_enemy_camp_active,
    is_natural_resource, is_depleted_resource_placeholder,
    is_regrowable_resource,
)
from get_game_config import get_game_config, get_level_from_xp, get_name_from_item_id, get_attribute_from_mission_id, get_xp_from_level, get_attribute_from_item_id, get_item_from_subcat_functional
from constants import Constant
from engine import apply_cost, apply_collect, apply_collect_xp, timestamp_now
from bundle import SAVES_DIR


def _same_local_day(ts_a: int, ts_b: int) -> bool:
    """Same LOCAL calendar day. The client's Utils.isSameDay compares local
    Date components, so the darts daily gate must be calendar-day based, not
    a rolling 24h window (a throw at 20:00 must not block the next morning's
    free game). A zero timestamp means "never"."""
    if ts_a <= 0 or ts_b <= 0:
        return False
    return datetime.date.fromtimestamp(ts_a) == datetime.date.fromtimestamp(ts_b)


def _utc_day(ts: int) -> int:
    """Epoch days in UTC. The client's Utils.isDailyBonusReady computes
    floor((ts/3600 + timeZone)/24) with timeZone unset (= 0), i.e. UTC day
    boundaries, so the daily login bonus must use the same arithmetic."""
    return int(ts) // 86400


def _current_darts_set_start(now_ts: int):
    """Local-midnight timestamp of the darts prize set covering `now_ts`, or
    None if the schedule has no current set. Mirrors the client, which parses
    start_date into a local Date and picks the set whose week contains now."""
    for entry in get_game_config().get("darts_items") or []:
        if not entry.get("items"):
            continue
        try:
            start_dt = datetime.datetime.strptime(entry["start_date"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        start = int(start_dt.timestamp())
        if start <= now_ts < start + 7 * 86400:
            return start
    return None


def _grant_resource(save, rtype, amount):
    """Add `amount` of a resource identified by its config type code
    (s=stone, w=wood, f=food, g=gold/coins, c=cash) to the default map /
    player. No-op for unknown types or non-positive amounts."""
    amount = int(amount)
    if amount <= 0 or not rtype:
        return
    m = save["maps"][0]
    if rtype == "s":
        m["stone"] = int(m.get("stone", 0)) + amount
    elif rtype == "w":
        m["wood"] = int(m.get("wood", 0)) + amount
    elif rtype == "f":
        m["food"] = int(m.get("food", 0)) + amount
    elif rtype == "g":
        m["coins"] = int(m.get("coins", 0)) + amount
    elif rtype == "c":
        save["playerInfo"]["cash"] = int(save["playerInfo"]["cash"]) + amount


def _grant_assist_reward(save, count):
    """Credit the neighbour-assist reward the client applies on completion.

    ASSIST_REWARD_GOLD/XP come from the game globals; `count` is the number of
    assisted buildings (1 for the single-bar flow). Gold and XP go to the
    player's default town, matching the client's local Base.Player.adjustStats.
    """
    count = max(0, int(count))
    if count <= 0:
        return
    globals_cfg = get_game_config().get("globals", {})
    try:
        gold = int(globals_cfg.get("ASSIST_REWARD_GOLD", 10) or 0)
        xp = int(globals_cfg.get("ASSIST_REWARD_XP", 3) or 0)
    except (TypeError, ValueError):
        gold, xp = 10, 3
    town_id = int(save["playerInfo"].get("default_map", 0) or 0)
    if town_id < 0 or town_id >= len(save["maps"]):
        town_id = 0
    town = save["maps"][town_id]
    town["coins"] = int(town.get("coins", 0) or 0) + gold * count
    town["xp"] = int(town.get("xp", 0) or 0) + xp * count


def _find_map_item(town, item_id, x, y):
    return next((
        item for item in town.get("items", [])
        if item and int(item[0]) == int(item_id)
        and item[1] == x and item[2] == y
    ), None)


def _item_attrs(item):
    """Return the persisted item attributes object, creating missing slots."""
    while len(item) < 6:
        item.append(0)
    if len(item) < 7:
        item.append([])
    if len(item) < 8:
        item.append({})
    elif not isinstance(item[7], dict):
        item[7] = {}
    return item[7]


def _social_item(item_id):
    return next((
        social for social in get_game_config().get("social_items", [])
        if int(social.get("id", -1)) == int(item_id)
    ), None)


def _social_worker_count(social):
    workers = str(social.get("workers", "") or "")
    return len([worker for worker in workers.split(",") if worker])


def _town_has_open_market(town):
    """True if the town has an operational market.

    A market that is a social building (Market I/III, Troll Market I/III) opens
    only once fully staffed (attrs.si == None). A market that is not a social
    building (e.g. Market II) has no helper requirement and is operational as
    soon as it is placed. Trading is refused unless at least one such market is
    open, so a reload cannot trade through an unstaffed market.
    """
    for it in town.get("items", []):
        try:
            item_id = int(it[0])
        except (TypeError, ValueError, IndexError):
            continue
        try:
            functional = int(get_attribute_from_item_id(item_id, "subcat_functional"))
        except (TypeError, ValueError):
            continue
        if functional != Constant.SUBCATFUNC_BUILDING_MARKET:
            continue
        if _social_item(item_id) is None:
            return True  # no staffing requirement (e.g. Market II)
        if _item_attrs(it).get("si", []) is None:
            return True  # staffed and opened
    return False


def _hire_social_friend(save, USERID, x, y, town_id, item_id, friend_id):
    """Fill one social-building role with an explicitly linked player."""
    friend_id = str(friend_id)
    if not is_friend(str(USERID), friend_id):
        print(f"Social hire rejected - {friend_id} is not a linked friend.")
        return False
    if town_id < 0 or town_id >= len(save["maps"]):
        return False
    town = save["maps"][town_id]
    item = _find_map_item(town, item_id, x, y)
    social = _social_item(item_id)
    if item is None or social is None:
        print("Social hire rejected - building/config not found.")
        return False
    attrs = _item_attrs(item)
    staff = attrs.setdefault("si", [])
    if staff is None:
        print("Social hire ignored - building is already open.")
        return False
    if not isinstance(staff, list):
        staff = []
        attrs["si"] = staff
    required = _social_worker_count(social)
    if friend_id in [str(value) for value in staff]:
        print(f"Social hire ignored - {friend_id} already works here.")
        return False
    if len(staff) >= required:
        print("Social hire ignored - staffing is already complete.")
        return False
    staff.append(friend_id)
    print(
        f"Hired friend {friend_id} ({len(staff)}/{required}) for "
        f"{get_name_from_item_id(item_id)}."
    )
    return True


_PRODUCTION_SECONDS = {
    1: 30 * 60,
    2: 60 * 60,
    3: 4 * 60 * 60,
    4: 8 * 60 * 60,
}


def _contained_worker_count(item):
    if len(item) > 6 and isinstance(item[6], list):
        return max(1, len(item[6]))
    return 1


def _start_production_cycle(item, now=None):
    """Snapshot the beginning of a mine/mill production cycle.

    `workerSeconds` is accumulated whenever staffing changes. This makes the
    final bonus proportional to actual participation instead of trusting the
    worker count sent by the Flash client at collection time.
    """
    attrs = _item_attrs(item)
    try:
        option = int(attrs.get("cp", 0) or 0)
    except (TypeError, ValueError):
        option = 0
    if option not in _PRODUCTION_SECONDS:
        attrs.pop("productionLabor", None)
        return None
    start = int(timestamp_now() if now is None else now)
    attrs["productionLabor"] = {
        "start": start,
        "last": start,
        "workers": _contained_worker_count(item),
        "workerSeconds": 0,
    }
    return attrs["productionLabor"]


def _update_production_labor(item, now=None):
    """Accumulate staffed time up to now or the cycle's completion."""
    attrs = _item_attrs(item)
    try:
        option = int(attrs.get("cp", 0) or 0)
        started = int(item[4] or 0)
    except (TypeError, ValueError, IndexError):
        return None
    duration = _PRODUCTION_SECONDS.get(option)
    if duration is None or started <= 0:
        return None
    cycle = attrs.get("productionLabor")
    if not isinstance(cycle, dict) or int(cycle.get("start", -1)) != started:
        # Saves created before labour accounting do not tell us when their
        # current workers entered. Count one baseline worker for that one
        # in-flight cycle instead of granting a full late-worker bonus.
        cycle = {
            "start": started,
            "last": started,
            "workers": 1,
            "workerSeconds": 0,
        }
        attrs["productionLabor"] = cycle
    current = int(timestamp_now() if now is None else now)
    horizon = min(max(current, started), started + duration)
    last = min(max(int(cycle.get("last", started) or started), started), horizon)
    workers = max(1, int(cycle.get("workers", 1) or 1))
    cycle["workerSeconds"] = max(
        0, int(cycle.get("workerSeconds", 0) or 0)
    ) + workers * max(0, horizon - last)
    cycle["last"] = horizon
    return cycle


def _set_production_workers(item):
    attrs = _item_attrs(item)
    cycle = attrs.get("productionLabor")
    if isinstance(cycle, dict):
        cycle["workers"] = _contained_worker_count(item)


def _effective_production_workers(item, now=None):
    """Average workers that participated before the resource became ready."""
    cycle = _update_production_labor(item, now)
    if not isinstance(cycle, dict):
        return float(_contained_worker_count(item))
    started = int(cycle["start"])
    horizon = int(cycle.get("last", started) or started)
    elapsed = max(0, horizon - started)
    if elapsed == 0:
        return float(max(1, int(cycle.get("workers", 1) or 1)))
    return max(
        1.0,
        float(cycle.get("workerSeconds", 0) or 0) / float(elapsed),
    )


def _unit_warehouse_state(town):
    """Return normalized Unit Warehouse capacity and unit-count mapping.

    The Flash client expects ``warehousedUnits`` to be an object whose keys
    are unit ids and values are counts. Some original/sample saves use an
    empty array instead, so accept that legacy representation too.
    """
    try:
        capacity = max(0, int(
            town.get("warehouseAditionalCapacitySingle", 0) or 0
        ))
    except (TypeError, ValueError):
        capacity = 0

    raw_units = town.get("warehousedUnits", {})
    if isinstance(raw_units, list):
        # An empty list is common in old saves. If a non-empty legacy list is
        # encountered, treat it as a list of unit ids rather than losing it.
        units = {}
        for raw_id in raw_units:
            try:
                unit_id = str(int(raw_id))
            except (TypeError, ValueError):
                continue
            units[unit_id] = units.get(unit_id, 0) + 1
    elif isinstance(raw_units, dict):
        units = {}
        for raw_id, raw_count in raw_units.items():
            try:
                unit_id = str(int(raw_id))
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                units[unit_id] = count
    else:
        units = {}

    town["warehouseAditionalCapacitySingle"] = capacity
    town["warehousedUnits"] = units
    return capacity, units


def _has_unit_warehouse(town):
    return any(
        item and int(item[0]) == Constant.ID_BUILDING_UNIT_WAREHOUSE
        for item in town.get("items", [])
    )


def _initialize_unit_warehouse(town):
    """A purchased/placed Warehouse includes its first usable unit slot."""
    capacity, units = _unit_warehouse_state(town)
    if _has_unit_warehouse(town) and capacity < 1:
        capacity = 1
        town["warehouseAditionalCapacitySingle"] = capacity
    return capacity, units


def _add_gifts(save, item_id, count=1):
    """Add item counts to the client's sparse, id-indexed storage array."""
    item_id = int(item_id)
    count = int(count)
    if item_id < 0 or count <= 0:
        return
    gifts = save["privateState"]["gifts"]
    while len(gifts) <= item_id:
        gifts.append(0)
    gifts[item_id] += count


def _battle_counts(row):
    """Return (item id, initial, killed, recovered) from a counted-array row."""
    if not isinstance(row, (list, tuple)) or not row:
        return None
    try:
        values = [int(row[i]) if i < len(row) else 0 for i in range(4)]
    except (TypeError, ValueError):
        return None
    return values


def _find_open_unit_position(town):
    """Find a deterministic free map tile for a unit reward."""
    occupied = {
        (int(item[1]), int(item[2]))
        for item in town.get("items", [])
        if item and len(item) >= 3
    }
    # Start around the initial Town Hall and expand in rings. Exact client
    # placement is cosmetic; persistence only needs a legal unoccupied tile.
    for radius in range(1, 40):
        for dx, dy in (
            (radius, 0), (-radius, 0), (0, radius), (0, -radius),
            (radius, radius), (-radius, radius),
            (radius, -radius), (-radius, -radius),
        ):
            pos = (52 + dx, 52 + dy)
            if 1 <= pos[0] < 99 and 1 <= pos[1] < 99 and pos not in occupied:
                return pos
    return (50, 50)


def _reconcile_battle_units(town, counted_units):
    """Persist casualties and explicit rescued/free units from a battle.

    The Flash counted array is [id, initial, killed, recovered]. Units already
    exist on the home map while the temporary quest/PvP map runs. Therefore
    only ``killed - recovered`` must be removed. A row with recovery greater
    than its killed count represents newly rescued/free units and is added.
    """
    if not isinstance(counted_units, list):
        return {"removed": 0, "added": 0}
    removed = added = 0
    for raw in counted_units:
        values = _battle_counts(raw)
        if values is None:
            continue
        unit_id, _initial, killed, recovered = values
        losses = max(0, killed - recovered)
        bonuses = max(0, recovered - killed)
        for _ in range(losses):
            victim = next((
                item for item in town.get("items", [])
                if item and int(item[0]) == unit_id
            ), None)
            if victim is None:
                break
            town["items"].remove(victim)
            removed += 1
        for _ in range(bonuses):
            x, y = _find_open_unit_position(town)
            town["items"].append([
                unit_id, x, y, 0, timestamp_now(), 0, [], {},
            ])
            added += 1
    return {"removed": removed, "added": added}


def _pvp_state(save):
    """Repair and return the persistent PvP fields consumed by the client."""
    state = save["privateState"]
    if not isinstance(state.get("attacksSent"), list):
        state["attacksSent"] = []
    if not isinstance(state.get("attacksReceived"), list):
        state["attacksReceived"] = []
    for key in ("attacksWon", "attacksLost", "honor", "tsAttacksReset"):
        try:
            state[key] = int(state.get(key, 0) or 0)
        except (TypeError, ValueError):
            state[key] = 0
    return state


def _pending_attack_entry(state, victim_id):
    victim_id = str(victim_id)
    for attack in reversed(state["attacksSent"]):
        if (
            str(attack.get("victim_id")) == victim_id
            and attack.get("description") is None
        ):
            return attack
    return None


def _log_unhandled(USERID, cmd, args):
    "Append an unimplemented command's payload for later implementation."
    try:
        line = json.dumps({"userid": USERID, "cmd": cmd, "args": args})
        with open(os.path.join(SAVES_DIR, "unhandled_commands.log"), "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f" [!] Could not log unhandled command '{cmd}': {e}")

def get_strategy_type(id):
    if id == 8:
        return "Defensive"
    if id == 9:
        return "Mid Defensive"
    if id == 7:
        return "Mid Aggressive"
    if id == 10:
        return "Aggressive"
    return "Unknown Strategy"

def command(USERID, data):
    timestamp = data["ts"]
    first_number = data["first_number"]
    accessToken = data["accessToken"]
    tries = data["tries"]
    publishActions = data["publishActions"]
    commands = data["commands"]
    initializing_resource_towns = set()
    
    try:
        i = 0
        while i < len(commands):
            comm = commands[i]
            cmd = comm["cmd"]
            args = comm["args"]

            # The client reports the initial wild trees/stone/gold as one
            # batch of free buys. Accept that batch once per town; later loads
            # must not repopulate harvested deposits at random coordinates.
            if (cmd == Constant.CMD_BUY and len(args) >= 6 and bool(args[5])
                    and is_natural_resource(args[0])):
                town_id = int(args[4])
                town = session(USERID)["maps"][town_id]
                if (int(town.get("naturalResourcesInitialized", 0) or 0)
                        and town_id not in initializing_resource_towns):
                    print(f" [+] COMMAND: buy({args}) -> Natural resource respawn ignored; map environment already initialized.")
                    i += 1
                    continue
                initializing_resource_towns.add(town_id)

            # PopupDarts queues a prize immediately before the shot which
            # earned it. Validate the shot first so a rejected paid throw
            # cannot still place its unit in storage.
            if (cmd == Constant.CMD_STORE_ADD_ITEMS and i + 1 < len(commands)
                    and commands[i + 1]["cmd"] == Constant.CMD_DARTS_SHOOT_BALLOON):
                shot = commands[i + 1]
                try:
                    accepted = do_command(USERID, shot["cmd"], shot["args"])
                    if accepted:
                        do_command(USERID, cmd, args)
                    else:
                        print("Darts: prize rejected with its invalid throw.")
                except Exception as e:
                    print(f" [!] Darts prize/shot pair failed: {type(e).__name__}: {e}. Skipping.")
                i += 2
                continue
            try:
                do_command(USERID, cmd, args)
            except Exception as e:
                # One bad command must not discard the rest of the batch.
                print(f" [!] Command '{cmd}' failed: {type(e).__name__}: {e}. Skipping.")
            i += 1
    finally:
        save = session(USERID)
        for town_id in initializing_resource_towns:
            save["maps"][town_id]["naturalResourcesInitialized"] = 1
        save_session(USERID) # Always persist successful mutations

def do_command(USERID, cmd, args):
    save = session(USERID)
    print (" [+] COMMAND: ", cmd, "(", args, ") -> ", sep='', end='')

    if cmd == Constant.CMD_GAME_STATUS:
        print(" ".join(args))

    elif cmd == Constant.CMD_BUY:
        id = args[0]
        x = args[1]
        y = args[2]
        frame = args[3] # TODO ??
        town_id = args[4]
        bool_dont_modify_resources = bool(args[5]) # 1 if the game "buys" for you, so does not substract whatever the item cost is.
        price_multiplier = args[6]
        type = args[7]
        if is_depleted_resource_placeholder(id):
            print("Obsolete stone/gold regeneration placeholder rejected.")
            return False
        print("Add", str(get_name_from_item_id(id)), "at", f"({x},{y})")
        collected_at_timestamp = timestamp_now()
        level = 0 # TODO 
        orientation = 0
        map = save["maps"][town_id]
        if not bool_dont_modify_resources:
            apply_cost(save["playerInfo"], map, id, price_multiplier)
            xp = int(get_attribute_from_item_id(id, "xp"))
            map["xp"] = map["xp"] + xp
        map["items"] += [[id, x, y, orientation, collected_at_timestamp, level]]
        if int(id) == Constant.ID_BUILDING_UNIT_WAREHOUSE:
            _initialize_unit_warehouse(map)
        if bool_dont_modify_resources and is_enemy_camp_marker(id):
            mark_enemy_camp_active(map, collected_at_timestamp)
        return True
    
    elif cmd == Constant.CMD_COMPLETE_TUTORIAL:
        tutorial_step = args[0]
        print("Tutorial step", tutorial_step, "reached.")
        if tutorial_step >= 31: # 31 is Dragon choosing. After that, you have some freedom. There's at least until step 45.
            print("Tutorial COMPLETED!")
            save["playerInfo"]["completed_tutorial"] = 1
            save["privateState"]["dragonNestActive"] = 1 
    
    elif cmd == Constant.CMD_MOVE:
        ix = args[0]
        iy = args[1]
        id = args[2]
        newx = args[3]
        newy = args[4]
        frame = args[5]
        town_id = args[6]
        reason = args[7] # "Unitat", "moveTo", "colisio", "MouseUsed"
        print("Move", str(get_name_from_item_id(id)), "from", f"({ix},{iy})", "to", f"({newx},{newy})")
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[0] == id and item[1] == ix and item[2] == iy:
                item[1] = newx
                item[2] = newy
                break
    
    elif cmd == Constant.CMD_COLLECT:
        x = args[0]
        y = args[1]
        town_id = args[2]
        id = args[3]
        resource_multiplier = int(args[5]) if len(args) > 5 else 1
        cash_to_substract = int(args[6]) if len(args) > 6 else 0
        print("Collect", str(get_name_from_item_id(id)))
        map = save["maps"][town_id]
        item = next((
            current for current in map["items"]
            if current[0] == id and current[1] == x and current[2] == y
        ), None)
        # A socially-gated producer (e.g. Stone Mine: Geologist + Miner) does
        # not produce until it is fully staffed and opened (attrs.si == None).
        # The client marks an open building with si=None; a missing key or a
        # (partial) list means the staffing window is still pending. Reject the
        # collect so a reload that drops the client-side overlay cannot harvest
        # an unstaffed building.
        if item is not None and _social_item(id) is not None:
            si_state = _item_attrs(item).get("si", [])
            if si_state is not None:
                print(f"Collect rejected - {get_name_from_item_id(id)} is not staffed/opened.")
                return False
        # PopupCollect time options use the same fixed multipliers in the
        # original Flash client: 30m, 1h, 4h, 8h => 0.5x, 1x, 2x, 3x.
        production_multiplier = 1
        worker_count = (
            float(_contained_worker_count(item)) if item is not None else 1.0
        )
        if item is not None and len(item) > 7 and isinstance(item[7], dict):
            try:
                collect_option = int(item[7].get("cp", 0) or 0)
            except (TypeError, ValueError):
                collect_option = 0
            if 1 <= collect_option <= 4:
                production_multiplier = (0.5, 1, 2, 3)[collect_option - 1]
                worker_count = _effective_production_workers(item)
        apply_collect(
            save["playerInfo"],
            map,
            id,
            resource_multiplier,
            worker_count,
            production_multiplier,
        )
        save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - cash_to_substract, 0)
        # Advance the item's collect timestamp so it enters cooldown and is not
        # re-offered after a reload (client computes "ready" from serverTime - item[4]).
        if item is not None:
            item[4] = timestamp_now()
            # Collection immediately starts the next cycle with the workers
            # currently assigned. Late additions affect that next cycle, not
            # the completed one.
            _start_production_cycle(item, item[4])

    elif cmd == Constant.CMD_SELL:
        x = args[0]
        y = args[1]
        id = args[2]
        town_id = args[3]
        bool_dont_modify_resources = args[4]
        reason = args[5]
        print("Remove", str(get_name_from_item_id(id)), "from", f"({x},{y}). Reason: {reason}")
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[0] == id and item[1] == x and item[2] == y:
                map["items"].remove(item)
                break
        # A fully harvested wild stone/gold deposit (the client's regrowth timer
        # was patched out of the SWF) is scheduled to reappear at the same tile
        # after TIMER_RESOURCE_REGEN_SECONDS. get_player_info re-populates it on
        # the next map load. Only natural depletion (HARV) regrows - a bulldoze
        # or upgrade sale of the same tile must not.
        if reason == Constant.SELL_REASON_HARVEST and is_regrowable_resource(id):
            pending = map.setdefault("pendingResourceRespawns", [])
            pending[:] = [
                p for p in pending
                if not (int(p.get("x")) == int(x) and int(p.get("y")) == int(y))
            ]
            pending.append({
                "id": int(id), "x": int(x), "y": int(y),
                "at": timestamp_now() + Constant.TIMER_RESOURCE_REGEN_SECONDS,
            })
        # Upgrades are represented by the client as ``sell(old, "UPGR")``
        # followed by ``buy(new)``. The client applies the normal 5% resale
        # credit locally before charging the next tier's full listed price, so
        # the authoritative save must do the same.
        if not bool_dont_modify_resources:
            price_multiplier = -0.05
            if get_attribute_from_item_id(id, "cost_type") != "c":
                apply_cost(save["playerInfo"], save["maps"][town_id], id, price_multiplier)
        if reason == 'KILL':
            pass # TODO : add to graveyard
    
    elif cmd == Constant.CMD_KILL:
        x = args[0]
        y = args[1]
        id = args[2]
        town_id = args[3]
        type = args[4]
        print("Kill", str(get_name_from_item_id(id)), "from", f"({x},{y}).")
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[0] == id and item[1] == x and item[2] == y:
                apply_collect_xp(map, id)
                map["items"].remove(item)
                break
    
    elif cmd == Constant.CMD_COMPLETE_MISSION:
        mission_id = args[0]
        skipped_with_cash = bool(args[1])
        print("Complete mission", mission_id, ":", str(get_attribute_from_mission_id(mission_id, "title")))
        if skipped_with_cash:
            cash_to_substract = 0 # TODO 
            save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - cash_to_substract, 0)
        save["privateState"]["completedMissions"] += [mission_id]
    
    elif cmd == Constant.CMD_REWARD_MISSION:
        town_id = args[0]
        mission_id = args[1]
        print("Reward mission", mission_id, ":", str(get_attribute_from_mission_id(mission_id, "title")))
        reward = int(get_attribute_from_mission_id(mission_id, "reward")) # gold
        save["maps"][town_id]["coins"] += reward   
        save["privateState"]["rewardedMissions"] += [mission_id]
    
    elif cmd == Constant.CMD_PUSH_UNIT:
        unit_x = args[0]
        unit_y = args[1]
        unit_id = args[2]
        b_x = args[3]
        b_y = args[4]
        town_id = args[5]
        print("Push", str(get_name_from_item_id(unit_id)), "to", f"({b_x},{b_y}).")
        map = save["maps"][town_id]
        # Unit into building
        for item in map["items"]:
            if item[1] == b_x and item[2] == b_y:
                _update_production_labor(item)
                if len(item) < 7:
                    item += [[]]
                item[6] += [unit_id]
                _set_production_workers(item)
                break
        # Remove unit
        for item in map["items"]:
            if item[0] == unit_id and item[1] == unit_x and item[2] == unit_y:
                map["items"].remove(item)
                break
    
    elif cmd == Constant.CMD_POP_UNIT:
        b_x = args[0]
        b_y = args[1]
        town_id = args[2]
        unit_id = args[3]
        place_popped_unit = len(args) > 4
        if place_popped_unit:
            unit_x = args[4]
            unit_y = args[5]
            unit_frame = args[6] # unknown use
        print("Pop", str(get_name_from_item_id(unit_id)), "from", f"({b_x},{b_y}).")
        map = save["maps"][town_id]
        # Remove unit from building
        for item in map["items"]:
            if item[1] == b_x and item[2] == b_y:
                if len(item) < 7:
                    break
                _update_production_labor(item)
                item[6].remove(unit_id)
                _set_production_workers(item)
                break
        if place_popped_unit:
            # Spawn unit outside
            collected_at_timestamp = timestamp_now()
            level = 0 # TODO 
            orientation = 0
            map["items"] += [[unit_id, unit_x, unit_y, orientation, collected_at_timestamp, level]]
    
    elif cmd == Constant.CMD_RT_LEVEL_UP:
        new_level = int(args[0])
        # Player level/xp is GLOBAL, not per-town: the client sends only the
        # level (no town id) because the original backend kept one value per
        # account. Persist it to the default town as the canonical store;
        # get_player_info mirrors it onto every town so switching towns can't
        # overwrite it (leveling in a second town used to drop the main town).
        map = save["maps"][int(save["playerInfo"].get("default_map", 0) or 0)]
        old_level = int(map.get("level", 0) or 0)
        # Level only ever goes up; the client sends rt_level_up on a level-UP.
        # Never accept a downgrade (a stale command from a lower-level town
        # would otherwise drop the whole account).
        if new_level <= old_level:
            print(f"Level Up ignored: {new_level} <= current {old_level}.")
            return
        print("Level Up!:", new_level)
        map["level"] = new_level
        current_xp = map["xp"]
        min_expected_xp = get_xp_from_level(max(0, new_level - 1))
        map["xp"] = max(min_expected_xp, current_xp) # try to fix problems with not counting XP... by keeping up with client-side level counting
        # Grant each newly-crossed level's configured reward exactly once.
        # Level N's reward lives at levels[N-1] (e.g. level 5 -> 1 cash). Using
        # the stored old_level as the baseline makes repeated calls idempotent
        # and covers multi-level jumps in a single XP change.
        levels = get_game_config()["levels"]
        for lvl in range(old_level + 1, new_level + 1):
            idx = lvl - 1
            if 0 <= idx < len(levels):
                r = levels[idx]
                _grant_resource(save, r.get("reward_type"), r.get("reward_amount", 0))
                print(f"  level {lvl} reward: {r.get('reward_amount')} '{r.get('reward_type')}'")

    elif cmd == Constant.CMD_RT_PUBLISH_SCORE:
        new_xp = int(args[0])
        print("xp set to", new_xp)
        # Global player xp - store on the default town (canonical). Never let a
        # second town's lower xp clobber it: the client sends whatever the
        # currently-loaded town shows, and with get_player_info now mirroring
        # the global xp onto every town, that value is always the global one.
        map = save["maps"][int(save["playerInfo"].get("default_map", 0) or 0)]
        # xp is monotonic; never let a stale/lower publish reduce it.
        new_xp = max(int(map.get("xp", 0) or 0), new_xp)
        map["xp"] = new_xp
        map["level"] = get_level_from_xp(new_xp)

    elif cmd == Constant.CMD_EXPAND:
        land_id = args[0]
        resource = args[1]
        town_id = int(args[2])
        print("Expansion", land_id, "purchased")
        map = save["maps"][town_id]
        if land_id in map["expansions"]:
            return
        # Substract resources
        expansion_prices = get_game_config()["expansion_prices"]
        exp = expansion_prices[len(map["expansions"]) - 1]
        if resource == "gold":
            to_substract = exp["coins"]
            save["maps"][town_id]["coins"] = max(save["maps"][town_id]["coins"] - to_substract, 0)
        elif resource == "cash":
            to_substract = exp["cash"]
            save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - to_substract, 0)
        # Add expansion
        map["expansions"].append(land_id)

    elif cmd == Constant.CMD_NAME_MAP:
        town_id =int(args[0])
        new_name = args[1]
        print(f"Map name changed to '{new_name}'.")
        save["playerInfo"]["map_names"][town_id] = new_name

    elif cmd == Constant.CMD_EXCHANGE_CASH:
        town_id = args[0]
        print("Exchange cash -> coins.")
        save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - 5, 0)#maybe make function for editing resources
        save["maps"][town_id]["coins"] += 2500

    elif cmd == Constant.CMD_TOURNAMENT_SUBSTRACT_RESOURCES or cmd == Constant.CMD_TOURNAMENT_REFUND_RESOURCES:
        # Tournament entry fee bookkeeping. The client subtracts the fee when
        # joining/creating and refunds it when the service answers NOK (this
        # server always does: no matchmaking). Mirror both so the save stays
        # in sync with what the client shows. Coins live per town; the
        # commands carry no town id, so main town stands in for the pair -
        # subtract and refund are symmetric, the net effect is zero.
        resource_type = args[0]  # "g" gold/coins, "c" cash
        amount = int(float(args[1]))
        if cmd == Constant.CMD_TOURNAMENT_SUBSTRACT_RESOURCES:
            amount = -amount
        print(f"Tournament {'refund' if amount >= 0 else 'fee'}: {abs(amount)} {resource_type}.")
        if resource_type == "c":
            save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] + amount, 0)
        else:
            save["maps"][0]["coins"] = max(save["maps"][0]["coins"] + amount, 0)

    elif cmd == Constant.CMD_BUY_WAREHOUSE_CAPACITY_NEW:
        # PopupUnitWarehouse buys one slot at a time for the configured cash
        # price and sends [town_id]. The client changes cash/capacity locally,
        # so the server must mirror both or refresh gives the cash back and
        # resets the slot.
        town_id = int(args[0])
        town = save["maps"][town_id]
        capacity, _ = _initialize_unit_warehouse(town)
        globals_ = get_game_config()["globals"]
        max_capacity = int(globals_.get("WAREHOUSE_MAX_CAPACITY", 1000))
        price = int(globals_.get(
            "WAREHOUSE_CAPACITY_INCREASE_PRICE_SINGLE", 2
        ))
        if not _has_unit_warehouse(town):
            print("Unit Warehouse not present - slot purchase rejected.")
            return False
        if capacity >= max_capacity:
            print("Unit Warehouse already at maximum capacity.")
            return False
        cash = int(save["playerInfo"].get("cash", 0) or 0)
        if cash < price:
            print("Not enough cash for a Unit Warehouse slot.")
            return False
        save["playerInfo"]["cash"] = cash - price
        town["warehouseAditionalCapacitySingle"] = capacity + 1
        print(
            "Unit Warehouse capacity:",
            f"{capacity} -> {capacity + 1}; cash -{price}.",
        )
        return True

    elif cmd == Constant.CMD_ADD_UNIT_WAREHOUSE:
        # IsoBuilding.pushUnitToWarehause sends
        # [unit_x, unit_y, town_id, unit_id]. Remove that exact deployed unit;
        # its absence from map.items is what frees its population.
        unit_x, unit_y = args[0], args[1]
        town_id, unit_id = int(args[2]), int(args[3])
        town = save["maps"][town_id]
        capacity, units = _initialize_unit_warehouse(town)
        if not _has_unit_warehouse(town):
            print("Unit Warehouse not present - store rejected.")
            return False
        if sum(units.values()) >= capacity:
            print("Unit Warehouse is full - store rejected.")
            return False
        if get_attribute_from_item_id(unit_id, "type") != "u":
            print("Only units can enter the Unit Warehouse.")
            return False
        deployed = _find_map_item(town, unit_id, unit_x, unit_y)
        if deployed is None:
            print("Deployed unit not found - store rejected.")
            return False
        town["items"].remove(deployed)
        key = str(unit_id)
        units[key] = units.get(key, 0) + 1
        print("Store", str(get_name_from_item_id(unit_id)), "in Unit Warehouse.")
        return True

    elif cmd == Constant.CMD_PLACE_WAREHOUSED_ITEM:
        # PopupUnitWarehouse sends [unit_id, x, y, orientation, town_id].
        # Consume the stored copy before spawning it to prevent free units.
        unit_id = int(args[0])
        x, y, orientation = args[1], args[2], int(args[3])
        town_id = int(args[4])
        town = save["maps"][town_id]
        _, units = _unit_warehouse_state(town)
        key = str(unit_id)
        if not _has_unit_warehouse(town):
            print("Unit Warehouse not present - deployment rejected.")
            return False
        if get_attribute_from_item_id(unit_id, "type") != "u":
            print("Warehouse item is not a unit - deployment rejected.")
            return False
        if units.get(key, 0) <= 0:
            print("Unit not in warehouse - deployment rejected.")
            return False
        units[key] -= 1
        if units[key] <= 0:
            units.pop(key, None)
        town["items"].append([
            unit_id, x, y, orientation, timestamp_now(), 0
        ])
        print("Deploy", str(get_name_from_item_id(unit_id)), "from Unit Warehouse.")
        return True

    elif cmd == Constant.CMD_RESET_WAREHOUSE:
        # When the Warehouse building is moved into normal storage, Base.as
        # transfers all its contained units to Gifts/Storage and then clears
        # the warehouse list. Mirror that server-side. Purchased capacity is
        # account/town progress and the client deliberately keeps it.
        town_id = int(args[0])
        town = save["maps"][town_id]
        _, units = _unit_warehouse_state(town)
        for raw_id, count in list(units.items()):
            _add_gifts(save, int(raw_id), int(count))
        moved = sum(units.values())
        town["warehousedUnits"] = {}
        print(f"Reset Unit Warehouse; moved {moved} unit(s) to storage.")
        return True

    elif cmd == Constant.CMD_STORE_ITEM or cmd == Constant.CMD_STORE_ITEM_FROMBUG:
        # store_item_frombug is the client relocating a colliding/out-of-bounds
        # item to storage; it uses the same [x, y, town, id] args as store_item.
        # If not persisted, the item stays on the map and the client re-sends this
        # every single load.
        x = args[0]
        y = args[1]
        town_id = int(args[2])
        item_id = args[3]
        print("Store", str(get_name_from_item_id(item_id)), "from", f"({x},{y})")
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[0] == item_id and item[1] == x and item[2] == y:
                map["items"].remove(item)
                break
        _add_gifts(save, item_id)

    elif cmd == Constant.CMD_STORE_ADD_ITEMS:
        # A batch of item ids to drop into storage (gifts). Used by darts prizes,
        # offer packs, etc. args[0] is a JSON-encoded array of item ids.
        item_ids = json.loads(args[0]) if args and args[0] else []
        for raw_id in item_ids:
            _add_gifts(save, int(raw_id))
        print("Store add items:", item_ids)

    elif cmd == Constant.CMD_DARTS_NEW_FREE:
        # The client sends this when its stored timeStampDartsNewFree is on an
        # earlier LOCAL calendar day: one free game per day. It does NOT clear
        # the board - dartsBalloonsShot is the whole week's progress and only
        # darts_reset (new weekly prize set) wipes it.
        pState = save["privateState"]
        now = timestamp_now()
        last_claim = int(pState.get("timeStampDartsNewFree", 0) or 0)
        if _same_local_day(last_claim, now) and not pState.get("dartsHasFree"):
            print("Darts: free game already used today - claim rejected.")
            return False
        print("Darts: claim daily free game.")
        pState["timeStampDartsNewFree"] = now
        pState["dartsHasFree"] = True
        return True

    elif cmd == Constant.CMD_DARTS_RESET:
        # Sent when a new weekly prize set started (client checks its stored
        # timeStampDartsReset against the set's start_date). Starts a fresh
        # board with a free throw. Only accepted once per weekly set so the
        # board can't be re-rolled at will.
        pState = save["privateState"]
        now = timestamp_now()
        set_start = _current_darts_set_start(now)
        if set_start is not None and int(pState.get("timeStampDartsReset", 0) or 0) >= set_start:
            print("Darts: board already reset for this week's set - rejected.")
            return False
        print("Darts: reset board for new weekly set.")
        pState["timeStampDartsReset"] = now
        pState["timeStampDartsNewFree"] = now
        pState["dartsBalloonsShot"] = []
        pState["dartsRandomSeed"] = int(args[0]) if args else 0
        pState["dartsHasFree"] = True
        return True

    elif cmd == Constant.CMD_DARTS_SHOOT_BALLOON:
        # The daily free game (claimed via darts_new_free) covers one throw;
        # every further throw is billed the configured DART_COST_CASH ("Play
        # again for 20"). The client only deducts that cash locally and never
        # sends the payment, so the server must charge it here or a reload
        # restores the cash and throws become infinite. Once the persisted
        # cash drops below the price, the client's own canAfford() check
        # blocks the board, so the daily limit holds.
        pState = save["privateState"]
        now = timestamp_now()
        if pState.get("dartsHasFree"):
            pState["dartsHasFree"] = False
            pState["timeStampLastDart"] = now
            print("Darts: free daily throw.")
        else:
            price = int(get_game_config()["globals"].get("DART_COST_CASH", 20))
            cash = int(save["playerInfo"]["cash"])
            if cash < price:
                print(f"Darts: extra throw rejected - needs {price} cash, has {cash}.")
                return False
            save["playerInfo"]["cash"] = cash - price
            pState["timeStampLastDart"] = now
            print(f"Darts: extra throw billed {price} cash ({cash} -> {cash - price}).")
        balloon_index = int(args[0])
        print("Darts: shoot balloon", balloon_index)
        if not isinstance(pState.get("dartsBalloonsShot"), list):
            pState["dartsBalloonsShot"] = []
        pState["dartsBalloonsShot"].append(balloon_index)
        return True

    elif cmd == Constant.CMD_PLACE_GIFT or cmd == Constant.CMD_PLACE_STORED_ITEM:
        # Both are [id, x, y, frame, town] (Base.finishPlacing). The town id is
        # the last element for BOTH commands; place_gift previously read args[3]
        # (the frame) as the town, so any non-zero frame placed the gift in the
        # wrong or an out-of-range town. Fall back to args[3] only for a legacy
        # 4-element command. Storage is checked BEFORE placing so a crash on the
        # decrement cannot persist a free item.
        item_id = int(args[0])
        x = args[1]
        y = args[2]
        town_id = int(args[4]) if len(args) > 4 else int(args[3])
        gifts = save["privateState"]["gifts"]
        if item_id < 0 or item_id >= len(gifts) or gifts[item_id] <= 0:
            print("None of", str(get_name_from_item_id(item_id)), "in storage - placement rejected.")
            return
        print("Add", str(get_name_from_item_id(item_id)), "at", f"({x},{y})")
        items = save["maps"][town_id]["items"]
        orientation = 0#TODO
        collected_at_timestamp = timestamp_now()
        level = 0
        items += [[item_id, x, y, orientation, collected_at_timestamp, level]]#maybe make function for adding items
        if item_id == Constant.ID_BUILDING_UNIT_WAREHOUSE:
            _initialize_unit_warehouse(save["maps"][town_id])
        gifts[item_id] -= 1
        while len(gifts) != 0 and gifts[-1] == 0: #removes excess zeros at end if necessary
            gifts.pop()

    elif cmd == Constant.CMD_SELL_GIFT or cmd == Constant.CMD_SELL_STORED:
        # Both are [id, town]: remove one unit from storage, refund 5% of its
        # (non-cash) cost. Guarded so selling items you don't own is rejected
        # instead of corrupting the gifts array.
        item_id = int(args[0])
        town_id = int(args[1])
        print("Gift", str(get_name_from_item_id(item_id)), "sold on town:",town_id)
        gifts = save["privateState"]["gifts"]
        if item_id < 0 or item_id >= len(gifts) or gifts[item_id] <= 0:
            print("None of", str(get_name_from_item_id(item_id)), "in storage - sale rejected.")
            return
        gifts[item_id] -= 1
        while len(gifts) != 0 and gifts[-1] == 0: #removes excess zeros at end if necessary
            gifts.pop()
        price_multiplier = -0.05
        if get_attribute_from_item_id(item_id, "cost_type") != "c":
            apply_cost(save["playerInfo"], save["maps"][town_id], item_id, price_multiplier)
    
    elif cmd == Constant.CMD_ACTIVATE_DRAGON:
        currency = args[0]
        print("Dragon nest activated.")
        if currency == 'c':
            save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - 50), 0)
        elif currency == 'g':
            map = save["maps"]
            map[0]["coins"] = max(int(map[0]["coins"] - 100000), 0)
        save["privateState"]["dragonNestActive"] = 1
        save["privateState"]["timeStampTakeCare"] = -1 # remove timer if any
    
    elif cmd == Constant.CMD_DESACTIVATE_DRAGON:
        print("Dragon nest deactivated.")
        pState = save["privateState"]
        pState["dragonNestActive"] = 0
        # reset step and dragon numbers
        pState["stepNumber"] = 0
        pState["dragonNumber"] = 0
        pState["timeStampTakeCare"] = -1 # remove timer if any

    elif cmd == Constant.CMD_NEXT_DRAGON_STEP:
        unknown = args[0]
        print("Dragon step increased.")
        pState = save["privateState"]
        pState["stepNumber"] += 1
        pState["timeStampTakeCare"] = timestamp_now()

    elif cmd == Constant.CMD_NEXT_DRAGON:
        print("Dragon step reset and dragonNumber increased.")
        pState = save["privateState"]
        pState["stepNumber"] = 0
        pState["dragonNumber"] += 1
        pState["timeStampTakeCare"] = -1 # remove timer

    elif cmd == Constant.CMD_DRAGON_BUY_STEP_CASH:
        price = args[0]
        print("Buy dragon step with cash.")
        save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - price), 0)
        save["privateState"]["timeStampTakeCare"] = -1 # remove timer

    elif cmd == Constant.CMD_RIDER_BUY_STEP_CASH:
        price = args[0]
        print("Buy rider step with cash.")
        save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - price), 0)
        save["privateState"]["riderTimeStamp"] = -1 # remove timer

    elif cmd == Constant.CMD_NEXT_RIDER_STEP:
        print("Rider step increased.")
        pState = save["privateState"]
        pState["riderStepNumber"] += 1
        pState["riderTimeStamp"] = timestamp_now()
    
    elif cmd == Constant.CMD_SELECT_RIDER:
        number = int(args[0])
        pState = save["privateState"]
        if number == 1 or number == 2 or number == 3:
            pState["riderNumber"] = number
            print("Rider", number, "Selected.")
        else:
            pState["riderNumber"] = 0
            pState["riderStepNumber"] = 0
            pState["riderTimeStamp"] = -1 # remove timer
            print("Rider reset.")
    
    elif cmd == Constant.CMD_ORIENT:
        x = args[0]
        y = args[1]
        new_orientation = args[2]
        town_id = args[3]
        print("Item at", f"({x},{y})", "changed to orientation", new_orientation)
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[1] == x and item[2] == y:
                item[3] = new_orientation
                break
    
    elif cmd == Constant.CMD_MONSTER_BUY_STEP_CASH:
        price = args[0]
        print("Buy monster step with cash.")
        save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - price), 0)
        save["privateState"]["timeStampTakeCareMonster"] = -1 # remove timer
    
    elif cmd == Constant.CMD_ACTIVATE_MONSTER:
        currency = args[0]
        print("Monster nest activated.")
        if currency == 'c':
            save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - 50), 0)
        elif currency == 'g':
            map = save["maps"]
            map[0]["coins"] = max(int(map[0]["coins"] - 100000), 0)
        save["privateState"]["monsterNestActive"] = 1
        save["privateState"]["timeStampTakeCareMonster"] = -1 # remove timer if any
    
    elif cmd == Constant.CMD_DESACTIVATE_MONSTER: # cmd called too late
        print("Monster nest deactivated.")
        pState = save["privateState"]
        pState["monsterNestActive"] = 0
        pState["stepMonsterNumber"] = 0
        pState["MonsterNumber"] = 0
        pState["timeStampTakeCareMonster"] = -1 # remove timer if any


    elif cmd == Constant.CMD_NEXT_MONSTER_STEP:
        print("Monster Step increased.")
        pState = save["privateState"]
        pState["stepMonsterNumber"] += 1
        pState["timeStampTakeCareMonster"] = timestamp_now()

    elif cmd == Constant.CMD_NEXT_MONSTER:
        print("Monster Step reset and Monster Number increased.")
        pState = save["privateState"]
        pState["stepMonsterNumber"] = 0
        pState["monsterNumber"] += 1
        pState["timeStampTakeCareMonster"] = -1 # remove timer

    elif cmd == Constant.CMD_WIN_BONUS:
        # Daily login bonus. Must mirror the client (PopupNewDaily +
        # Utils.isDailyBonusReady) exactly or the reward shown on screen
        # differs from the one persisted:
        #  - gate by UTC CALENDAR DAY, not a rolling 24h (a 23:50 claim must
        #    not block the 00:10 popup the client will show);
        #  - reward index displayed today is (bonusNextId - 1) % 5, resetting
        #    to day 1 when a day was skipped or on the first ever claim;
        #  - on hero days the client picks a RANDOM hero from
        #    DAILY_BONUS_CONFIG_HEROES, shows it and passes it in args[2], so
        #    honor that id (whitelisted) or storage won't match the popup.
        # The client-submitted coins/cash amounts stay ignored (they were the
        # infinite-cash exploit) - resources always come from the config.
        town_id = int(args[1]) if len(args) > 1 else 0
        client_hero = int(float(args[2])) if len(args) > 2 else 0
        pState = save["privateState"]
        now = timestamp_now()
        last = int(pState.get("timestampLastBonus", 0) or 0)
        day_diff = _utc_day(now) - _utc_day(last)
        if last and day_diff < 1:
            print("Daily bonus already claimed today - rejected.")
            return

        cfg = get_game_config()["globals"]["DAILY_BONUS_CONFIG"]
        next_id = int(pState.get("bonusNextId", 0) or 0)
        if not last or next_id <= 0 or day_diff > 1:
            idx = 0  # first ever claim, or streak broken: back to day 1
        else:
            idx = (next_id - 1) % len(cfg)
        reward = cfg[idx]
        qty = int(reward.get("qty", 0) or 0)
        rtype = reward.get("type")
        print(f"Claiming daily bonus [{idx}]: {qty} '{rtype}'")

        map = save["maps"][town_id]
        if rtype == "g":
            map["coins"] = int(map["coins"]) + qty
        elif rtype == "c":
            save["playerInfo"]["cash"] = int(save["playerInfo"]["cash"]) + qty
        elif rtype == "hero":
            heroes = [int(h) for h in get_game_config()["globals"]["DAILY_BONUS_CONFIG_HEROES"]]
            hero_id = client_hero if client_hero in heroes else heroes[0]
            gifts = pState["gifts"]
            while len(gifts) <= hero_id:
                gifts.append(0)
            gifts[hero_id] += qty
            print(f"  daily bonus hero ID={hero_id} x{qty}")

        # Next login the client displays (bonusNextId - 1) % 5, so storing
        # idx + 2 advances the popup to the following day.
        pState["bonusNextId"] = idx + 2
        pState["timestampLastBonus"] = now

    elif cmd == Constant.CMD_ADMIN_ADD_ANIMAL:
        subcatFunc = str(args[0])
        toBeAdded = int(args[1])
        print("Added", toBeAdded, get_item_from_subcat_functional(subcatFunc)["name"])

        # TODO
        oAnimals: dict = save["privateState"]["arrayAnimals"]
        oAnimals[subcatFunc] = toBeAdded + (oAnimals[subcatFunc] if subcatFunc in oAnimals else 0)
    
    elif cmd == Constant.CMD_GRAVEYARD_BUY_POTIONS:
        # no args
        print("Graveyard buy potion")
        # info from config
        graveyard_potions = get_game_config()["globals"]["GRAVEYARD_POTIONS"]
        amount = graveyard_potions["amount"]
        price_cash = graveyard_potions["price"]["c"]
        # pay
        save["playerInfo"]["cash"] = max(int(save["playerInfo"]["cash"] - price_cash), 0)
        # add potion
        save["privateState"]["potion"] += amount

    elif cmd == Constant.CMD_RESURRECT_HERO:
        # Two client flows share this command:
        #  - PopupGraveyard (5 args, last is "1"/"0"): pays with potions
        #    (unit's `potion` attribute) or, without potion, with the unit's
        #    resource cost (gold units: food=cost + gold=cost/2; cash units:
        #    cash=cost/2).
        #  - PopupResurrectHeroes (4 args): pays gold = cost_unit_cash * 500
        #    (Config.RESURRECT_MULTIPLIER).
        # The client only deducts locally, so the server must charge the same
        # or resurrection is free after a reload.
        unit_id = int(args[0])
        x = args[1]
        y = args[2]
        town_id = int(args[3])
        map = save["maps"][town_id]
        print("Resurrect", str(get_name_from_item_id(unit_id)), "from graveyard")
        if len(args) > 4:
            if str(args[4]) == '1':
                needed = int(get_attribute_from_item_id(unit_id, "potion") or 1)
                potions = int(save["privateState"]["potion"])
                if potions < needed:
                    print(f"Resurrect rejected - needs {needed} potions, has {potions}.")
                    return
                save["privateState"]["potion"] = potions - needed
            else:
                cost = int(get_attribute_from_item_id(unit_id, "cost") or 0)
                if get_attribute_from_item_id(unit_id, "cost_type") == "c":
                    price = round(cost / 2)
                    cash = int(save["playerInfo"]["cash"])
                    if cash < price:
                        print(f"Resurrect rejected - needs {price} cash, has {cash}.")
                        return
                    save["playerInfo"]["cash"] = cash - price
                else:
                    food_price, gold_price = cost, round(cost / 2)
                    food, coins = int(map["food"]), int(map["coins"])
                    if food < food_price or coins < gold_price:
                        print(f"Resurrect rejected - needs {food_price} food + {gold_price} gold.")
                        return
                    map["food"] = food - food_price
                    map["coins"] = coins - gold_price
        else:
            price = int(get_attribute_from_item_id(unit_id, "cost_unit_cash") or 0) * 500
            coins = int(map["coins"])
            if coins < price:
                print(f"Resurrect rejected - needs {price} gold, has {coins}.")
                return
            map["coins"] = coins - price
        # Place unit
        collected_at_timestamp = timestamp_now()
        level = 0 # TODO
        orientation = 0
        map["items"] += [[unit_id, x, y, orientation, collected_at_timestamp, level]]

    elif cmd == Constant.CMD_BUY_SUPER_OFFER_PACK:
        town_id = args[0]
        unknown2 = args[1] # this is probably the super offer pack ID?
        items = args[2]
        cash_used = args[3]
        
        map = save["maps"][town_id]

        item_array = items.split(',')
        for item in item_array:
            item_id = int(item)
            length = len(save["privateState"]["gifts"])
            if length <= item_id:
                for i in range(item_id - length + 1):
                    save["privateState"]["gifts"].append(0)
            save["privateState"]["gifts"][item_id] += 1

        save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - cash_used, 0)#maybe make function for editing resources
        print(f"Used {cash_used} cash to buy super offer pack!")

    elif cmd == Constant.CMD_SET_STRATEGY:
        strategy_type = args[0]
        type_name = get_strategy_type(strategy_type)
        save["privateState"]["strategy"] = strategy_type
        print(f"Set defense strategy type to {type_name}")

    elif cmd == Constant.CMD_ATTACK_PLAYER:
        victim_id = str(args[0])
        state = _pvp_state(save)
        now = timestamp_now()
        recent = [
            attack for attack in state["attacksSent"]
            if now - int(attack.get("time", 0) or 0) < 6 * 3600
        ]
        same_target = any(
            str(attack.get("victim_id")) == victim_id
            and now - int(attack.get("time", 0) or 0) < 4 * 3600
            for attack in state["attacksSent"]
        )
        if victim_id == str(USERID) or neighbor_session(victim_id) is None:
            state["pendingAttackRejected"] = victim_id
            print(f"Attack rejected - unknown/self target {victim_id}.")
            return False
        if len(recent) >= 3 or same_target:
            state["pendingAttackRejected"] = victim_id
            print("Attack rejected - attack limit or opponent cooldown.")
            return False
        entry = {"time": now, "victim_id": victim_id}
        state["attacksSent"].append(entry)
        # Ten entries are enough for the client history popup and prevent
        # unbounded save growth while preserving the active 6-hour window.
        state["attacksSent"] = state["attacksSent"][-20:]
        state["pendingAttackTarget"] = victim_id
        state.pop("pendingAttackRejected", None)
        print(f"Attack started against {victim_id}.")
        return True

    elif cmd == Constant.CMD_CLEAN_ATTACKS:
        state = _pvp_state(save)
        for attack in state["attacksReceived"]:
            if isinstance(attack, dict):
                attack.pop("viewPending", None)
        print("Marked received attacks as viewed.")
        return True

    elif cmd == Constant.CMD_START_QUEST:
        quest_id = args[0]
        town_id = args[1]
        print(f"Start quest {quest_id}")

    elif cmd == Constant.CMD_END_QUEST:
        data = json.loads(args[0])
        town_id = data["map"]
        gold_gained = data["resources"]["g"]
        xp_gained = data["resources"]["x"]
        units = data["units"]
        win = data["win"] == 1
        duration_sec = data["duration"]
        voluntary_end = data["voluntary_end"] == 1
        quest_id = int(data["quest_id"])
        item_rewards = data["item_rewards"] if "item_rewards" in data else None
        activators_left = data["activators_left"] if "activators_left" in data else None
        difficulty = data["difficulty"]

        # Resources
        save["maps"][town_id]["coins"] += int(gold_gained)
        save["maps"][town_id]["xp"] += int(xp_gained)
        unit_result = _reconcile_battle_units(save["maps"][town_id], units)

        # Update quests data
        save["privateState"]["unlockedQuestIndex"] = max(quest_id + 1, save["privateState"]["unlockedQuestIndex"], 0)
        # Star rank: questsRank is keyed by the quest id string (matches the
        # client's ISLE_ORDER lookup) and holds the best difficulty cleared. The
        # island shows that many stars, so it must persist on a win.
        if win:
            quest_key = str(data["quest_id"])
            ranks = save["privateState"].get("questsRank")
            if not isinstance(ranks, dict):
                ranks = {}
            ranks[quest_key] = max(int(ranks.get(quest_key, 0)), int(difficulty))
            save["privateState"]["questsRank"] = ranks
        # save["maps"]["questTimes"] [quest_id] = TODO min (... , duration_sec)
        # save["maps"]["lastQuestTimes"] [quest_id] = TODO min (... , duration_sec)

        print(
            f"Ended quest {quest_id}.", "WIN" if win else "loss",
            f"difficulty {difficulty}; casualties {unit_result['removed']}, "
            f"rescued {unit_result['added']}",
        )

    elif cmd == Constant.CMD_UNIT_COLLECTION_COMPLETED:
        collection_id = args[0]
        print("Unit collection completed:", collection_id)
        pState = save["privateState"]
        if not isinstance(pState.get("unitCollectionsCompleted"), list):
            pState["unitCollectionsCompleted"] = []
        # +1 cash reward, but only the first time this collection is completed
        # (the client shows sendCommand=false, so no apply_rewards_ranking fires
        # for collections; the cash must be granted here or it is lost).
        if collection_id not in pState["unitCollectionsCompleted"]:
            pState["unitCollectionsCompleted"].append(collection_id)
            save["playerInfo"]["cash"] = save["playerInfo"]["cash"] + 1

    elif cmd == Constant.CMD_APPLY_REWARDS_RANKING:
        level = args[0]
        cash = int(args[1]) if len(args) > 1 else 0
        items = json.loads(args[2]) if len(args) > 2 and args[2] else []
        print("Apply ranking reward: +", cash, "cash,", len(items), "items")
        save["playerInfo"]["cash"] = save["playerInfo"]["cash"] + cash
        gifts = save["privateState"]["gifts"]
        for raw_id in items:
            item_id = int(raw_id)
            while len(gifts) <= item_id:
                gifts.append(0)
            gifts[item_id] += 1

    elif cmd == Constant.CMD_END_ATTACK:
        data = json.loads(args[0])
        win = data.get("win") == 1
        resources = data.get("resources", {})
        gold_gained = int(resources.get("g", 0))
        xp_gained = int(resources.get("x", 0))
        state = _pvp_state(save)
        victim = data.get("victim") or {}
        victim_id = str(
            victim.get("user_id")
            or state.get("pendingAttackTarget")
            or ""
        )
        rejected = str(state.get("pendingAttackRejected", ""))
        if victim_id and rejected == victim_id:
            print(f"End attack ignored - start against {victim_id} was rejected.")
            state.pop("pendingAttackRejected", None)
            return False

        town_id = int(save["playerInfo"].get("default_map", 0) or 0)
        if town_id >= len(save["maps"]):
            town_id = 0
        map = save["maps"][town_id]
        map["coins"] += gold_gained
        map["xp"] += xp_gained
        casualties = _reconcile_battle_units(
            map, data.get("attacker_units") or []
        )
        if not victim_id:
            # Older clients and the enemy-camp return flow reuse end_attack
            # without a player opponent. Apply the reported battle result, but
            # do not consume a PvP attempt or create a blank history card.
            print(
                "End non-player battle.", "WIN" if win else "loss",
                f"(+{gold_gained}g, +{xp_gained}xp, "
                f"{casualties['removed']} permanent casualties)",
            )
            return True

        # History uses the original client's misspelled `oponent` field.
        description = dict(data)
        description["oponent"] = victim
        entry = _pending_attack_entry(state, victim_id)
        if entry is None:
            entry = {"time": timestamp_now(), "victim_id": victim_id}
            state["attacksSent"].append(entry)
        entry["description"] = description
        state["attacksWon" if win else "attacksLost"] += 1
        try:
            state["honor"] += int(data.get("honor", 0) or 0)
        except (TypeError, ValueError):
            pass
        state.pop("pendingAttackTarget", None)

        # Store the corresponding defender history and casualties when the
        # target is another writable local player. Static scenario villages
        # are read-only and only produce attacker-side history.
        defender = session(victim_id) if victim_id else None
        if defender is not None and victim_id != str(USERID):
            defender_state = _pvp_state(defender)
            defender_description = dict(description)
            defender_description["win"] = 0 if win else 1
            defender_description["oponent"] = data.get("attacker") or {
                "user_id": str(USERID),
                "name": save["playerInfo"].get("name", "Emperor"),
            }
            defender_state["attacksReceived"].append({
                "time": timestamp_now(),
                "victim_id": victim_id,
                "description": defender_description,
                "viewPending": 1,
            })
            defender_state["attacksReceived"] = defender_state["attacksReceived"][-20:]
            defender_state["attacksLost" if win else "attacksWon"] += 1
            defender_map_id = int(victim.get("map", 0) or 0)
            if defender_map_id < 0 or defender_map_id >= len(defender["maps"]):
                defender_map_id = 0
            _reconcile_battle_units(
                defender["maps"][defender_map_id],
                data.get("victim_units") or [],
            )
            save_session(victim_id)

        print(
            "End attack.", "WIN" if win else "loss",
            f"(+{gold_gained}g, +{xp_gained}xp, "
            f"{casualties['removed']} permanent casualties)",
        )
        # On a win, record the conquered island position so the PvP map shows it
        # complete. The client reads map["universAttackWin"] to mark conquered slots.
        if win:
            victim = data.get("victim") or {}
            posicion = victim.get("posicion")
            if posicion is not None:
                if not isinstance(map.get("universAttackWin"), list):
                    map["universAttackWin"] = []
                if int(posicion) not in map["universAttackWin"]:
                    map["universAttackWin"].append(int(posicion))

    elif cmd == Constant.CMD_ASSIST_NEIGHBOUR:
        # [friend_pid, lastIdAssist, town]. The player assisted a neighbour's
        # building. FauxBar2 credits ASSIST_REWARD_GOLD + ASSIST_REWARD_XP
        # locally on completion, so persist the same reward or it is lost on
        # reload. (Small fixed reward; the client gates how often you assist.)
        _grant_assist_reward(save, 1)
        return True

    elif cmd == Constant.CMD_ASSIST_NEIGHBOUR_NEW:
        # [friend_id, 0, json([[tx,ty,building_id], ...])]. Batched neighbour
        # assists collected while visiting. The reward scales with the number
        # of buildings helped, bounded so a malformed batch cannot farm.
        clicks = []
        try:
            if len(args) > 2:
                clicks = json.loads(args[2])
        except (ValueError, TypeError):
            clicks = []
        count = len(clicks) if isinstance(clicks, list) else 0
        if count > 0:
            _grant_assist_reward(save, min(count, 30))
        return True

    elif cmd == Constant.CMD_ASSIST_RECEIVE:
        # [town, building_id]. Acknowledge an ally-cart reward box. The box's
        # resource amount is not carried in the command (the flying token is a
        # client-side visual), so no server grant is applied; handling the
        # command keeps it from being dropped as unhandled.
        return True

    elif cmd == Constant.CMD_HIRE_WORKER:
        # Local replacement for the original Facebook callback:
        # [x, y, town, building, friend_id]. Only explicitly linked players
        # may fill a role and one friend can fill at most one slot per building.
        if len(args) < 5:
            print("Social hire rejected - malformed args.")
            return False
        return _hire_social_friend(
            save, USERID, args[0], args[1], int(args[2]), int(args[3]), args[4]
        )

    elif cmd == Constant.CMD_ASSIST_SEND_FEED:
        # Round Table request: [x, y, town, building, friend_id]. Only a real
        # friend may be targeted. The original Facebook acceptance callback is
        # unavailable in the local game, so a linked local friend accepts the
        # role immediately and both request/staff state survive a refresh.
        if len(args) < 5:
            print("Social feed rejected - malformed args.")
            return False
        x, y, town_id, item_id = args[0], args[1], int(args[2]), int(args[3])
        friend_id = str(args[4])
        if not is_friend(USERID, friend_id):
            print(f"Social feed rejected - {friend_id} is not a friend.")
            return False
        town = save["maps"][town_id]
        item = _find_map_item(town, item_id, x, y)
        if item is None or item_id != Constant.ID_BUILDING_ROUND_TABLE:
            print("Social feed rejected - Round Table not found.")
            return False
        attrs = _item_attrs(item)
        sent = attrs.get("sif")
        if isinstance(sent, list):
            sent = {str(value): True for value in sent}
        elif not isinstance(sent, dict):
            sent = {}
        if friend_id not in sent and len(sent) >= int(
            get_game_config()["globals"].get("MAX_FEEDS_ROUND_TABLE", 8)
        ):
            print("Social feed rejected - daily request limit reached.")
            return False
        sent[friend_id] = True
        attrs["sif"] = sent
        _hire_social_friend(
            save, USERID, x, y, town_id, item_id, friend_id
        )
        print(f"Social help request accepted by {friend_id}.")
        return True

    elif cmd == Constant.CMD_BUY_SI_HELP:
        # [x, y, town, item]. The client deducts 2 cash and appends a zero
        # placeholder locally; persist both or refresh reopens the staffing
        # window with every worker missing.
        x, y, town_id, item_id = args[0], args[1], int(args[2]), int(args[3])
        town = save["maps"][town_id]
        item = _find_map_item(town, item_id, x, y)
        social = _social_item(item_id)
        if item is None or social is None:
            print("Social worker purchase rejected - building/config not found.")
            return False
        attrs = _item_attrs(item)
        if "si" in attrs and attrs["si"] is None:
            # null is the client's marker for an already-opened building.
            print("Social worker purchase ignored - building already opened.")
            return False
        staff = attrs.setdefault("si", [])
        if not isinstance(staff, list):
            staff = []
            attrs["si"] = staff
        required = _social_worker_count(social)
        if len(staff) >= required:
            print("Social worker purchase ignored - staffing already complete.")
            return False
        price = int(social.get("worker_cost", 0) or 0)
        cash = int(save["playerInfo"].get("cash", 0) or 0)
        if cash < price:
            print(f"Social worker purchase rejected - needs {price} cash, has {cash}.")
            return False
        save["playerInfo"]["cash"] = cash - price
        staff.append(0)
        print(f"Bought worker {len(staff)}/{required} for {get_name_from_item_id(item_id)}.")
        return True

    elif cmd == Constant.CMD_FINISH_SI:
        # [x, y, town, item, ...]. Once all worker slots are filled the
        # client sets attrs.si=null and routes future clicks to the real
        # building popup instead of PopupSocialBuilding.
        x, y, town_id, item_id = args[0], args[1], int(args[2]), int(args[3])
        town = save["maps"][town_id]
        item = _find_map_item(town, item_id, x, y)
        social = _social_item(item_id)
        if item is None or social is None:
            print("Social building finish rejected - building/config not found.")
            return False
        attrs = _item_attrs(item)
        staff = attrs.setdefault("si", [])
        required = _social_worker_count(social)
        if staff is None:
            print("Social building already opened.")
            return True
        if not isinstance(staff, list) or len(staff) < required:
            print(f"Social building finish rejected - {len(staff) if isinstance(staff, list) else 0}/{required} workers.")
            return False
        if item_id == Constant.ID_BUILDING_SUMMIT or len(args) > 4:
            # Summit/social-feed helpers are a repeatable reward cycle. The
            # client clears both arrays after collection instead of opening
            # the building permanently.
            if item_id == Constant.ID_BUILDING_ROUND_TABLE:
                count = len(staff)
                gold = 1000 if count >= 4 else 0
                xp = 100 if count >= 6 else 0
                town["coins"] = int(town.get("coins", 0)) + gold
                town["xp"] = int(town.get("xp", 0)) + xp
                allowed = {
                    Constant.ID_UNIT_XENA,
                    Constant.ID_UNIT_ARTHUR,
                    Constant.ID_UNIT_RANGER,
                    Constant.ID_UNIT_MERLIN,
                }
                requested = int(args[6]) if len(args) > 6 else 0
                if count >= 8 and requested in allowed:
                    _add_gifts(save, requested)
                print(
                    f"Round Table reward: {gold} gold, {xp} xp"
                    + (f", unit {requested}" if count >= 8 and requested in allowed else "")
                )
            attrs["si"] = []
            attrs["sif"] = []
            print(f"Reset completed social helper cycle for {get_name_from_item_id(item_id)}.")
            return True
        attrs["si"] = None
        print(f"Opened staffed social building {get_name_from_item_id(item_id)}.")
        return True

    elif cmd == Constant.CMD_SET_RESOURCE_ALLIES:
        # [resource, x, y, town, item]. This choice belongs to the map, while
        # the hire list remains on the building item.
        resource, x, y = str(args[0]), args[1], args[2]
        town_id, item_id = int(args[3]), int(args[4])
        if resource not in ("f", "w", "s", "g"):
            print(f"Allies Market resource '{resource}' rejected.")
            return False
        town = save["maps"][town_id]
        item = _find_map_item(town, item_id, x, y)
        if item is None:
            print("Allies Market resource rejected - building not found.")
            return False
        town["resourceAlliesMarket"] = resource
        item[4] = 0  # the first collection is ready, matching the client
        print(f"Allies Market resource set to '{resource}'.")
        return True

    elif cmd == Constant.CMD_TRADE_RESOURCE:
        # [town, resource, sell, amount]. Reproduce ButtonMarket.refreshCost:
        # base * amount/100, then 2% per prior trade pressure; selling pays 75%.
        town_id, resource = int(args[0]), str(args[1])
        selling, amount = bool(int(args[2])), int(args[3])
        if resource not in ("f", "w", "s") or amount <= 0:
            print("Market trade rejected - invalid resource/amount.")
            return False
        town = save["maps"][town_id]
        if not _town_has_open_market(town):
            print("Market trade rejected - no staffed/open market in town.")
            return False
        now = timestamp_now()
        last = int(town.get("timestampLastTrade", 0) or 0)
        if last and now - last >= 20 * 3600:
            town["numTradesDone"] = 0
            town["resourcesTraded"] = {}
        done = int(town.get("numTradesDone", 0) or 0)
        if done >= 20:
            print("Market trade rejected - daily trade limit reached.")
            return False
        pressure_by_resource = town.setdefault("resourcesTraded", {})
        pressure = int(pressure_by_resource.get(resource, 0) or 0)
        base_cost = int(get_game_config()["globals"]["MARKET_BASE_COSTS"][resource])
        cost = math.floor((base_cost * amount / 100.0) * (1 + pressure * 0.02) + 0.5)
        if selling:
            cost = math.floor(cost * 0.75 + 0.5)
            current = int(town[{"f": "food", "w": "wood", "s": "stone"}[resource]])
            if current < amount:
                print(f"Market sale rejected - needs {amount} '{resource}', has {current}.")
                return False
            town[{"f": "food", "w": "wood", "s": "stone"}[resource]] = current - amount
            town["coins"] = int(town["coins"]) + cost
            pressure = max(-25, pressure - 1)
        else:
            coins = int(town["coins"])
            if coins < cost:
                print(f"Market purchase rejected - needs {cost} gold, has {coins}.")
                return False
            town["coins"] = coins - cost
            key = {"f": "food", "w": "wood", "s": "stone"}[resource]
            town[key] = int(town[key]) + amount
            pressure = min(200, pressure + 1)
        pressure_by_resource[resource] = pressure
        town["numTradesDone"] = done + 1
        town["timestampLastTrade"] = now
        print(f"Market {'sold' if selling else 'bought'} {amount} '{resource}' for {cost} gold.")
        return True

    elif cmd == Constant.CMD_ADD_COLLECTABLE:
        # A collectible drop (from PvP, harvesting, etc). args =
        # [collection_id, collectible_index]. Persist the count in
        # privateState.collections (per-collection [completedFlag, count,
        # count, ...]) so earned collectibles survive a reload - this was a
        # no-op TODO, so they were shown client-side then lost.
        try:
            collection_id = int(args[0])
            collectible_id = int(args[1])
        except (ValueError, IndexError, TypeError):
            print("add_collectable: bad args", args)
            return
        pState = save["privateState"]
        collections = pState.setdefault("collections", [])
        while len(collections) <= collection_id:
            collections.append([0])
        coll = collections[collection_id]
        while len(coll) <= collectible_id:
            coll.append(0)
        coll[collectible_id] += 1
        print(f"Collectible stored: collection {collection_id}, item {collectible_id} (x{coll[collectible_id]}).")

    elif cmd == Constant.CMD_ACTIVATE:
        x = args[0]
        y = args[1]
        town_id = args[2]
        item_id = args[3]
        time_option = args[4] if len(args) > 4 else 0
        print("Activate", str(get_name_from_item_id(item_id)), "at", f"({x},{y})", "option", time_option)
        map = save["maps"][town_id]
        for item in map["items"]:
            if item[0] == item_id and item[1] == x and item[2] == y:
                # An item is [id, x, y, orient, collected_at, level, units, attrs].
                # The client shows a producer as "working" only when attrs.cp is a
                # nonzero time option (1..4), and derives the production duration and
                # remaining time from attrs.cp + collected_at. Persist both so the
                # timer keeps running across reloads.
                while len(item) < 6:
                    item.append(0)          # level
                if len(item) < 7:
                    item.append([])         # units
                if len(item) < 8 or not isinstance(item[7], dict):
                    if len(item) < 8:
                        item.append({})     # attrs
                    else:
                        item[7] = {}
                item[7]["cp"] = time_option
                if time_option:
                    item[4] = timestamp_now()   # production start
                    _start_production_cycle(item, item[4])
                else:
                    item[7].pop("productionLabor", None)
                break

    elif cmd == Constant.CMD_COLLECT_MONDAY_BONUS or cmd == Constant.CMD_COLLECT_COMEBACK_BONUS:
        # weekly_reward / comeback_reward, args [type, value(, day)]. The
        # client picks a random reward and applies it locally; the server
        # validates the TYPE against the configured reward set and grants the
        # configured amount (never the client's), or stores the unit if it is
        # in the configured unit pool.
        rtype = str(args[0])
        rvalue = int(float(args[1] or 0))
        pState = save["privateState"]
        now = timestamp_now()
        g = get_game_config()["globals"]
        if cmd == Constant.CMD_COLLECT_MONDAY_BONUS:
            # Client shows the popup on Mondays, once per local day.
            if datetime.date.fromtimestamp(now).weekday() != 0:
                print("Monday bonus rejected - not Monday.")
                return
            if _same_local_day(int(pState.get("timeStampMondayBonus", 0) or 0), now):
                print("Monday bonus already collected today - rejected.")
                return
            rewards, units = g["MONDAY_BONUS_REWARDS"], g["MONDAY_BONUS_UNITS"]
        else:
            # Comeback bonus: one claim per streak day index (args[2]).
            day = int(args[2]) if len(args) > 2 else 0
            collected = pState.setdefault("comebackBonusCollected", [])
            if day in collected:
                print(f"Comeback bonus day {day} already collected - rejected.")
                return
            rewards, units = g["COMEBACK_BONUS_REWARDS"], g["COMEBACK_BONUS_UNITS"]
        cfg_reward = next((r for r in rewards if r.get("type") == rtype), None)
        if cfg_reward is None:
            print(f"Bonus reward type '{rtype}' not in config - rejected.")
            return
        if rtype == "u":
            unit_ids = [int(u) for u in units]
            unit_id = rvalue if rvalue in unit_ids else unit_ids[0]
            gifts = pState["gifts"]
            while len(gifts) <= unit_id:
                gifts.append(0)
            gifts[unit_id] += 1
            print(f"{cmd}: stored unit {unit_id}.")
        elif rtype == "g":
            qty = int(cfg_reward.get("value", 0))
            save["maps"][0]["coins"] = int(save["maps"][0]["coins"]) + qty
            print(f"{cmd}: +{qty} gold.")
        elif rtype == "c":
            qty = int(cfg_reward.get("value", 0))
            save["playerInfo"]["cash"] = int(save["playerInfo"]["cash"]) + qty
            print(f"{cmd}: +{qty} cash.")
        if cmd == Constant.CMD_COLLECT_MONDAY_BONUS:
            pState["timeStampMondayBonus"] = now
        else:
            pState["comebackBonusCollected"].append(day)

    elif cmd == Constant.CMD_COLLECT_TREASURE:
        # Quest-island treasure: [gold, xp, nextQuestId, food, stone, town].
        # Amounts are computed client-side from level tables, so clamp them to
        # the client's own maxima instead of trusting arbitrary values.
        gold = max(0, min(int(float(args[0] or 0)), 1500))
        xp = max(0, min(int(float(args[1] or 0)), 200))
        quest_id = int(args[2])
        food = max(0, min(int(float(args[3] or 0)), 1500))
        stone = max(0, min(int(float(args[4] or 0)), 1500))
        town_id = int(args[5]) if len(args) > 5 else 0
        map = save["maps"][town_id]
        map["coins"] = int(map["coins"]) + gold
        map["food"] = int(map["food"]) + food
        map["stone"] = int(map["stone"]) + stone
        map["xp"] = int(map["xp"]) + xp
        map["idCurrentTreasure"] = quest_id
        # Stamp the kill time. The client gates the enemy camp and its 4h
        # respawn countdown on now - map.timestampLastTreasure vs
        # TIMER_OGRES_VILLAGE (Base.as reads it on load, MapInitializer.Init
        # respawns the camp when the timer hits 0). Without persisting this a
        # reload sees 0 and respawns the camp the player just cleared.
        map["enemyCampActive"] = 0
        map["timestampLastTreasure"] = timestamp_now()
        print(f"Treasure collected: +{gold}g +{xp}xp +{food}f +{stone}s, next quest {quest_id}. Stamped {map['timestampLastTreasure']}.")

    elif cmd == Constant.CMD_BUY_UNIT_WITH_CASH:
        # [item_id, x, y, frame, town]: buy a unit paying its cash price
        # (cost_unit_cash) instead of resources.
        item_id = int(args[0])
        x = args[1]
        y = args[2]
        town_id = int(args[4]) if len(args) > 4 else 0
        price = int(get_attribute_from_item_id(item_id, "cost_unit_cash") or 0)
        cash = int(save["playerInfo"]["cash"])
        if cash < price:
            print(f"Buy with cash rejected - needs {price} cash, has {cash}.")
            return
        save["playerInfo"]["cash"] = cash - price
        map = save["maps"][town_id]
        map["items"] += [[item_id, x, y, 0, timestamp_now(), 0]]
        print("Bought", str(get_name_from_item_id(item_id)), f"for {price} cash at ({x},{y}).")

    elif cmd == Constant.CMD_BUY_MAP:
        # Buy a second town. args: [count(=1), resource(0=gold, 1=cash),
        # race("h"/"t"), currentTownID]. The client (PopupRaceSelector)
        # deducts locally and immediately travels to town 1, so the server
        # must actually create maps[1] or the follow-up load 500s and the
        # game hangs on the loading bar.
        resource = int(args[1])
        race = str(args[2])
        cur_town = int(args[3]) if len(args) > 3 else 0
        TOWN_PRICE_GOLD, TOWN_PRICE_CASH, TROLL_MIN_LEVEL = 100000, 22, 20
        if len(save["maps"]) >= 2:
            print("Second town already owned - buy_map rejected.")
            return
        if race == "t" and int(save["maps"][cur_town].get("level", 1)) < TROLL_MIN_LEVEL:
            print(f"Troll town needs level {TROLL_MIN_LEVEL} - buy_map rejected.")
            return
        if resource == 1:
            cash = int(save["playerInfo"]["cash"])
            if cash < TOWN_PRICE_CASH:
                print(f"Not enough cash for second town ({cash}/{TOWN_PRICE_CASH}) - rejected.")
                return
            save["playerInfo"]["cash"] = cash - TOWN_PRICE_CASH
        else:
            coins = int(save["maps"][cur_town]["coins"])
            if coins < TOWN_PRICE_GOLD:
                print(f"Not enough gold for second town ({coins}/{TOWN_PRICE_GOLD}) - rejected.")
                return
            save["maps"][cur_town]["coins"] = coins - TOWN_PRICE_GOLD
        # Create the town and register it so the client can list/switch to it.
        new_town = fresh_town_map(race)
        # Player level is global: start the new town at the account's current
        # level/xp so it doesn't show as level 1 before the next load.
        cur = save["maps"][cur_town]
        new_town["xp"], new_town["level"] = cur.get("xp", 0), cur.get("level", 1)
        save["maps"].append(new_town)
        pi = save["playerInfo"]
        base_name = str(pi["map_names"][0]) if pi.get("map_names") else "My Empire"
        pi.setdefault("map_names", []).append(f"{base_name} II")
        sizes = pi.setdefault("map_sizes", [])
        first_size = sizes[0] if sizes else 1
        sizes.append(first_size if first_size else 1)
        print(f"Second town created (race '{race}'), paid via {'cash' if resource == 1 else 'gold'}.")

    elif cmd == Constant.CMD_BAHAMUT_SUPREME_INVOCATION_TEMPLE_NEXT_STEP:
        # Complete one of the 12 Supreme Bahamut Temple steps. The client
        # deducts the cost locally then sends [json(data), stepIndex]; persist
        # it so a reload can neither undo the contribution nor replay it.
        # data is a resource cost {"g":n}/{"s":n}/{"w":n}/{"f":n}/{"c":n}/
        # {"mana":n}, a dragon sacrifice {"u": dragonId} (the unit was already
        # removed client-side), or {"collection": id}. templeStep is the list
        # of completed step indices; timeStampTemple starts the 48h wait.
        pState = save["privateState"]
        steps = pState.setdefault("templeStep", [])
        try:
            data = json.loads(args[0]) if args and args[0] else {}
            step = int(args[1])
        except (ValueError, IndexError, TypeError):
            print("Temple: malformed next_temple_step args", args)
            return
        if step in steps:
            # Idempotent: a completed step is never charged or recorded twice
            # (fixes double-submit granting the reward / spending resources
            # more than once).
            print(f"Temple: step {step} already done - ignored.")
            return
        default = int(save["playerInfo"].get("default_map", 0) or 0)
        maps = save["maps"]
        town = maps[default] if 0 <= default < len(maps) else maps[0]
        pinfo = save["playerInfo"]
        for currency, amount in data.items():
            try:
                amount = int(amount)
            except (ValueError, TypeError):
                continue  # "u"/"collection": consumed client-side, not a resource
            if currency == "g":
                town["coins"] = max(int(town.get("coins", 0)) - amount, 0)
            elif currency == "w":
                town["wood"] = max(int(town.get("wood", 0)) - amount, 0)
            elif currency == "s":
                town["stone"] = max(int(town.get("stone", 0)) - amount, 0)
            elif currency == "f":
                town["food"] = max(int(town.get("food", 0)) - amount, 0)
            elif currency == "c":
                pinfo["cash"] = max(int(pinfo["cash"]) - amount, 0)
            elif currency == "mana":
                pState["mana"] = max(int(pState.get("mana", 0)) - amount, 0)
        steps.append(step)
        pState["timeStampTemple"] = timestamp_now()
        print(f"Temple: step {step} completed ({data}). {len(steps)}/12 done.")

    elif cmd == Constant.CMD_BAHAMUT_SUPREME_INVOCATION_TEMPLE_BUY_TIME:
        # Skip the active 48h inter-step wait for 5 cash. The client already
        # zeroed timeStampTemple locally; mirror the charge and the reset so a
        # reload can't restore the wait or the cash.
        price = int(args[0]) if args else 5
        pinfo = save["playerInfo"]
        pinfo["cash"] = max(int(pinfo["cash"]) - price, 0)
        save["privateState"]["timeStampTemple"] = 0
        print(f"Temple: skipped step wait for {price} cash.")

    elif cmd == Constant.CMD_BAHAMUT_SUPREME_INVOCATION_TEMPLE_RESET:
        # Restart the temple (also fired right after the final Bahamut is
        # granted, which is what makes the reward one-shot: clearing templeStep
        # removes the completed state so it can't be claimed again).
        pState = save["privateState"]
        pState["templeStep"] = []
        pState["timeStampTemple"] = 0
        print("Temple: progress reset.")

    elif cmd == Constant.CMD_INCREASE_POPULATION:
        # Recruitment complete (hired the required friends): raise the town's
        # population limit, up to POPULATION_INCREASE_QTY times. Persist the
        # per-town increasedPopulation counter (client reads map
        # increasedPopulation); was unhandled, so the raise reverted on reload.
        town_id = int(args[0]) if args else int(save["playerInfo"].get("default_map", 0) or 0)
        if town_id < 0 or town_id >= len(save["maps"]):
            town_id = int(save["playerInfo"].get("default_map", 0) or 0)
        town = save["maps"][town_id]
        cap = int(get_game_config()["globals"].get("POPULATION_INCREASE_QTY", 5))
        town["increasedPopulation"] = min(int(town.get("increasedPopulation", 0)) + 1, cap)
        print(f"Population limit raised (town {town_id}): +{town['increasedPopulation']} step(s).")

    else:
        print(f"Unhandled command '{cmd}' -> args", args)
        _log_unhandled(USERID, cmd, args)
        return
    
