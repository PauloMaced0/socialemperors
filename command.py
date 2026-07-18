import json
import os
import datetime

from sessions import session, save_session
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
    
    try:
        for i, comm in enumerate(commands):
            cmd = comm["cmd"]
            args = comm["args"]
            try:
                do_command(USERID, cmd, args)
            except Exception as e:
                # One bad command must not discard the rest of the batch.
                print(f" [!] Command '{cmd}' failed: {type(e).__name__}: {e}. Skipping.")
    finally:
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
        num_units_contained_when_harvested = args[4]#TODO does this affect multiplier?
        resource_multiplier = args[5]
        cash_to_substract = args[6]
        print("Collect", str(get_name_from_item_id(id)))
        map = save["maps"][town_id]
        apply_collect(save["playerInfo"], map, id, resource_multiplier)
        save["playerInfo"]["cash"] = max(save["playerInfo"]["cash"] - cash_to_substract, 0)
        # Advance the item's collect timestamp so it enters cooldown and is not
        # re-offered after a reload (client computes "ready" from serverTime - item[4]).
        for item in map["items"]:
            if item[0] == id and item[1] == x and item[2] == y:
                item[4] = timestamp_now()
                break

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
                if len(item) < 7:
                    item += [[]]
                item[6] += [unit_id]
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
                item[6].remove(unit_id)
                break
        if place_popped_unit:
            # Spawn unit outside
            collected_at_timestamp = timestamp_now()
            level = 0 # TODO 
            orientation = 0
            map["items"] += [[unit_id, unit_x, unit_y, orientation, collected_at_timestamp, level]]
    
    elif cmd == Constant.CMD_RT_LEVEL_UP:
        new_level = int(args[0])
        map = save["maps"][0] # TODO : xp must be general, since theres no given town_id
        old_level = int(map.get("level", 0) or 0)
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
        new_xp = args[0]
        print("xp set to", new_xp)
        map = save["maps"][0] # TODO : xp must be general, since theres no given town_id
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
        length = len(save["privateState"]["gifts"])
        if length <= item_id:
            for i in range(item_id - length + 1):
                save["privateState"]["gifts"].append(0)
        save["privateState"]["gifts"][item_id] += 1

    elif cmd == Constant.CMD_STORE_ADD_ITEMS:
        # A batch of item ids to drop into storage (gifts). Used by darts prizes,
        # offer packs, etc. args[0] is a JSON-encoded array of item ids.
        item_ids = json.loads(args[0]) if args and args[0] else []
        gifts = save["privateState"]["gifts"]
        for raw_id in item_ids:
            item_id = int(raw_id)
            while len(gifts) <= item_id:
                gifts.append(0)
            gifts[item_id] += 1
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
            return
        print("Darts: claim daily free game.")
        pState["timeStampDartsNewFree"] = now
        pState["dartsHasFree"] = True

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
            return
        print("Darts: reset board for new weekly set.")
        pState["timeStampDartsReset"] = now
        pState["timeStampDartsNewFree"] = now
        pState["dartsBalloonsShot"] = []
        pState["dartsRandomSeed"] = int(args[0]) if args else 0
        pState["dartsHasFree"] = True

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
                return
            save["playerInfo"]["cash"] = cash - price
            pState["timeStampLastDart"] = now
            print(f"Darts: extra throw billed {price} cash ({cash} -> {cash - price}).")
        balloon_index = int(args[0])
        print("Darts: shoot balloon", balloon_index)
        if not isinstance(pState.get("dartsBalloonsShot"), list):
            pState["dartsBalloonsShot"] = []
        pState["dartsBalloonsShot"].append(balloon_index)

    elif cmd == Constant.CMD_PLACE_GIFT or cmd == Constant.CMD_PLACE_STORED_ITEM:
        # place_gift: [id, x, y, town, ?] - place_stored_item: [id, x, y, frame, town].
        # Both take one unit out of storage (gifts) and put it on the map. The
        # storage check must happen BEFORE the item is placed: placing first
        # and crashing on the decrement would persist a free item.
        item_id = int(args[0])
        x = args[1]
        y = args[2]
        town_id = int(args[4]) if cmd == Constant.CMD_PLACE_STORED_ITEM else int(args[3])
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

        print(f"Ended quest {quest_id}.", "WIN" if win else "loss", f"difficulty {difficulty}")

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
        town_id = save["playerInfo"].get("default_map", 0)
        if town_id >= len(save["maps"]):
            town_id = 0
        map = save["maps"][town_id]
        map["coins"] += gold_gained
        map["xp"] += xp_gained
        print("End attack.", "WIN" if win else "loss", f"(+{gold_gained}g, +{xp_gained}xp)")
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

    elif cmd == Constant.CMD_ADD_COLLECTABLE:
        collection_id = args[0]
        collectible_id = args[1]
        # TODO

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

    else:
        print(f"Unhandled command '{cmd}' -> args", args)
        _log_unhandled(USERID, cmd, args)
        return
    
