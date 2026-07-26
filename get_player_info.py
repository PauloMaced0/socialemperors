import copy
import datetime
import random

from sessions import (
    session, save_session, neighbor_session, neighbors,
    refresh_enemy_camp_timer, _display_name, _repair_social_building_state,
    _repair_natural_resource_state,
)
from engine import timestamp_now
from constants import Constant
from get_game_config import get_attribute_from_item_id, get_level_from_xp


def _ensure_town_list(save):
    """Populate privateState.maps, the town list the client feeds to
    TownManager.init(). TownManager.hasSecondTown() does `maps.length > 1`
    and getSecondRace() reads `maps[1].r`, so a missing field makes the
    client crash (#1009 null.length) the moment the player opens the Town /
    race-selector popup - the screen dims and nothing happens.

    Each entry is one owned town carrying its race code ("h"/"t"). Rebuilt
    from the authoritative maps array on every load so it can never drift."""
    save["privateState"]["maps"] = [
        {"r": m.get("race", "h")} for m in save["maps"]
    ]


def _sync_global_level(save):
    """Mirror the player's global xp/level onto every town. The client shows
    the CURRENT town's map.xp/level as the player level, and levelling sends
    no town id (level is a single per-account value in the original game). If
    towns kept independent xp, entering a fresh second town would show its
    low level and levelling there would overwrite the main town. Keep the
    default town as canonical and copy its xp/level to the others."""
    maps = save["maps"]
    default = int(save["playerInfo"].get("default_map", 0) or 0)
    if default < 0 or default >= len(maps):
        default = 0
    canonical = maps[default]
    xp = max(0, int(canonical.get("xp", 0) or 0))
    # The level field in old saves can drift from XP.  The HUD uses both
    # values to choose its min/max thresholds, so normalize the level before
    # the client computes the within-level progress bar.
    level = max(1, int(get_level_from_xp(xp)))
    canonical["xp"], canonical["level"] = xp, level
    for town in maps:
        if town is not canonical:
            town["xp"], town["level"] = xp, level


def _same_local_day(ts_a, ts_b):
    if int(ts_a or 0) <= 0 or int(ts_b or 0) <= 0:
        return False
    return datetime.date.fromtimestamp(int(ts_a)) == datetime.date.fromtimestamp(int(ts_b))


def _refresh_daily_animal_budget(save, now):
    """Reset the client's per-subcategory spawn allowance once per local day.

    Existing animals remain persisted map items, so the client only creates
    animals which are actually missing, capped by ANIMALS_PER_DAY.
    """
    pstate = save["privateState"]
    last = int(pstate.get("timestampAnimalsReset", 0) or 0)
    if last > 0 and not _same_local_day(last, now):
        pstate["arrayAnimals"] = {}
    pstate.setdefault("arrayAnimals", {})
    pstate["timestampAnimalsReset"] = int(now)


def _respawn_mature_trees(save, now):
    """Restore harvested trees after the natural-resource cooldown.

    Trees have no visible regeneration object, so their cooldown is persisted
    here. If a player built on a depleted tile, the pending tree is consumed
    rather than reappearing through the building.
    """
    changed = False
    tree_ids = {
        Constant.ID_BUILDING_TREE_1,
        Constant.ID_BUILDING_TREE_2,
        Constant.ID_BUILDING_TREE_3,
    }
    for town in save.get("maps", []):
        pending = town.get("pendingTreeRespawns")
        if not isinstance(pending, list) or not pending:
            continue
        items = town.setdefault("items", [])
        tree_count = sum(
            1 for item in items
            if item and int(item[0]) in tree_ids
        )
        remaining = []
        for entry in pending:
            try:
                tree_id = int(entry["id"])
                x, y = int(entry["x"]), int(entry["y"])
                ready_at = int(entry["at"])
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if tree_id not in tree_ids:
                changed = True
                continue
            if int(now) < ready_at:
                remaining.append(entry)
                continue
            occupied = any(
                item and int(item[1]) == x and int(item[2]) == y
                for item in items
            )
            if not occupied and tree_count < 300:
                items.append([tree_id, x, y, 0, int(now), 0])
                tree_count += 1
            changed = True
        if remaining != pending:
            town["pendingTreeRespawns"] = remaining
    return changed


_MINERAL_RESPAWN_IDS = {
    "gold": (
        Constant.ID_BUILDING_GOLD_1,
        Constant.ID_BUILDING_GOLD_2,
        Constant.ID_BUILDING_GOLD_3,
        Constant.ID_BUILDING_GOLD_4,
    ),
    "stone": (
        Constant.ID_BUILDING_STONE_1,
        Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3,
        Constant.ID_BUILDING_STONE_4,
    ),
}


def _occupied_map_tiles(town):
    """Tiles occupied by persisted item footprints."""
    occupied = set()
    for item in town.get("items", []):
        if not item or len(item) < 3:
            continue
        try:
            item_id, x, y = int(item[0]), int(item[1]), int(item[2])
            width = max(
                1, int(get_attribute_from_item_id(item_id, "width") or 1)
            )
            height = max(
                1, int(get_attribute_from_item_id(item_id, "height") or 1)
            )
            orientation = int(item[3] or 0) if len(item) > 3 else 0
        except (TypeError, ValueError):
            continue
        if orientation % 2:
            width, height = height, width
        for tx in range(x, x + width):
            for ty in range(y, y + height):
                if 0 <= tx < 100 and 0 <= ty < 100:
                    occupied.add((tx, ty))
    return occupied


def _random_wild_resource_position(town, occupied, excluded):
    """Choose an empty tile from the same wild regions as MapInitializer.

    The stock map is a 5x5 grid of 20x20 big tiles. Natural deposits are
    placed on the permanent outer border plus inner tiles the player has not
    bought. Excluding ``town.expansions`` reproduces that rule and prevents a
    respawn from occupying owned/buildable land.
    """
    try:
        owned = {int(value) for value in town.get("expansions", [])}
    except (TypeError, ValueError):
        owned = {13}
    big_tiles = [value for value in range(1, 26) if value not in owned]
    if not big_tiles:
        return None

    def candidate(big_tile, local_x, local_y):
        column = (big_tile - 1) % 5
        row = (big_tile - 1) // 5
        return column * 20 + local_x, row * 20 + local_y

    # Random first, matching the original spawn style. The deterministic
    # fallback guarantees that an unusually crowded map does not make a
    # matured timer disappear merely because random probing missed a gap.
    for _ in range(512):
        position = candidate(
            random.choice(big_tiles),
            random.randrange(20),
            random.randrange(20),
        )
        if position not in occupied and position != excluded:
            return position
    for big_tile in big_tiles:
        for local_y in range(20):
            for local_x in range(20):
                position = candidate(big_tile, local_x, local_y)
                if position not in occupied and position != excluded:
                    return position
    return None


def _respawn_mature_minerals(save, now):
    """Replace depleted gold/stone at a new random wild-map position.

    Each harvested node owns one persisted three-hour timer. The timer is
    removed only when a replacement is created (or its family is already at
    the stock hard cap), so reloads cannot accelerate or duplicate respawns.
    """
    changed = False
    for town in save.get("maps", []):
        pending = town.get("pendingMineralRespawns")
        if not isinstance(pending, list) or not pending:
            continue
        items = town.setdefault("items", [])
        occupied = _occupied_map_tiles(town)
        family_counts = {
            family: sum(
                1 for item in items
                if item and int(item[0]) in family_ids
            )
            for family, family_ids in _MINERAL_RESPAWN_IDS.items()
        }
        remaining = []
        for entry in pending:
            try:
                family = str(entry["family"])
                source = (
                    int(entry["source_x"]),
                    int(entry["source_y"]),
                )
                ready_at = int(entry["at"])
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if family not in _MINERAL_RESPAWN_IDS:
                changed = True
                continue
            if int(now) < ready_at:
                remaining.append(entry)
                continue
            if family_counts[family] >= 21:
                # Another legitimate population/respawn already filled this
                # family. Discard the surplus timer rather than exceeding the
                # stock three-cluster (3 x 5-7) maximum.
                changed = True
                continue
            position = _random_wild_resource_position(
                town, occupied, source
            )
            if position is None:
                remaining.append(entry)
                continue
            resource_id = random.choice(_MINERAL_RESPAWN_IDS[family])
            items.append([
                resource_id,
                position[0],
                position[1],
                0,
                int(now),
                0,
            ])
            occupied.add(position)
            family_counts[family] += 1
            changed = True
        if remaining != pending:
            town["pendingMineralRespawns"] = remaining
    return changed


def _sync_natural_resource_reload_marker(save, map_idx):
    """Prevent MapInitializer from repopulating resources on every reload.

    The patched client checks arrayAnimals[SUBCATFUNC_RESOURCE_REGEN] around
    only its tree/mineral population blocks. Animals keep their independent
    daily allowances. A new town is left unmarked until its first environment
    batch is persisted; established towns use server-authoritative pending
    tree/mineral cooldowns.
    """
    animals = save["privateState"].setdefault("arrayAnimals", {})
    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    town = save["maps"][map_idx]
    tutorial_done = int(
        save["playerInfo"].get("completed_tutorial", 0) or 0
    )
    initialized = int(town.get("naturalResourcesInitialized", 0) or 0)
    if tutorial_done and initialized:
        animals[marker] = 1
    else:
        animals.pop(marker, None)


def _clamp_map(save, map_number):
    """A valid map index for this village. Switching towns (or clicking
    "Town" with one town) requests map 1; an out-of-range index would 500
    and, because the client's map-load error handler is a no-op, freeze the
    game on the loading bar. Fall back to the default map."""
    maps = save["maps"]
    default = int(save["playerInfo"].get("default_map", 0) or 0)
    if map_number is None or map_number < 0 or map_number >= len(maps):
        map_number = default if 0 <= default < len(maps) else 0
    return map_number

def get_player_info(USERID, map_number=None):
    # Update last logged in
    ts_now = timestamp_now()
    save = session(USERID)
    # Server startup repairs legacy saves, but a building can also be placed
    # while this process is already running.  The Flash client treats a
    # missing ``attrs.si`` differently from an explicit empty list: missing
    # means open, while [] means "still needs every worker".  Normalize on
    # every player load so a browser refresh can never bypass an entirely
    # unfilled Market, Stone Mine, or other staffed building.
    social_state_changed = _repair_social_building_state(save)
    natural_state_changed = _repair_natural_resource_state(save)
    _refresh_daily_animal_budget(save, ts_now)
    trees_changed = _respawn_mature_trees(save, ts_now)
    minerals_changed = _respawn_mature_minerals(save, ts_now)
    save["playerInfo"]["last_logged_in"] = ts_now
    # dartsHasFree means "free game claimed (darts_new_free) but not yet
    # thrown". The client reads it at login and, on a new local day, claims a
    # fresh free game itself - so don't recompute it here, just repair stale
    # saves where a throw happened after the claim but the flag was never
    # consumed (pre-fix saves), so a reload can't hand out a bonus free throw.
    pState = save["privateState"]
    if "timeStampLastDart" not in pState:
        pState["timeStampLastDart"] = int(pState.get("timeStampDartsNewFree", 0) or 0)
    last_claim = int(pState.get("timeStampDartsNewFree", 0) or 0)
    last_dart = int(pState.get("timeStampLastDart", 0) or 0)
    if pState.get("dartsHasFree") and last_claim > 0 and last_dart >= last_claim:
        pState["dartsHasFree"] = False
    # Supreme Bahamut Temple: ensure the fields the client reads on load exist,
    # so older saves get valid Temple state automatically (the client does
    # Utils.inArray(step, privateState["templeStep"]) and would break on an
    # undefined list). templeStep = completed step indices; timeStampTemple =
    # last-step time that gates the 48h wait.
    pState.setdefault("templeStep", [])
    pState.setdefault("timeStampTemple", 0)
    # PvP client code iterates these lists and performs arithmetic on the
    # counters during every load; missing/null legacy values break histories,
    # cooldowns and goal progress.
    if not isinstance(pState.get("attacksSent"), list):
        pState["attacksSent"] = []
    if not isinstance(pState.get("attacksReceived"), list):
        pState["attacksReceived"] = []
    for pvp_key in (
        "attacksWon", "attacksLost", "honor", "tsAttacksReset",
        "attacksPack", "spyingsPack", "tsSpyingsReset",
    ):
        try:
            pState[pvp_key] = int(pState.get(pvp_key, 0) or 0)
        except (TypeError, ValueError):
            pState[pvp_key] = 0
    if not isinstance(pState.get("spyings"), list):
        pState["spyings"] = []
    # The local server has no separate Facebook display name, so the editable
    # default-town name is the player's identity. Keep it synchronized after
    # every rename; otherwise cards stay stuck on "Emperor" or an older name.
    pi = save["playerInfo"]
    names = pi.get("map_names") or []
    dm = int(pi.get("default_map", 0) or 0)
    if 0 <= dm < len(names) and names[dm]:
        pi["name"] = names[dm]
    # Market state is a 20-hour period in the Flash client.
    for m in save["maps"]:
        try:
            trades_done = int(m.get("numTradesDone", 0) or 0)
        except (TypeError, ValueError):
            trades_done = 0
        m["numTradesDone"] = min(max(trades_done, 0), 20)
        try:
            m["timestampLastTrade"] = max(
                0, int(m.get("timestampLastTrade", 0) or 0)
            )
        except (TypeError, ValueError):
            m["timestampLastTrade"] = 0
        if not isinstance(m.get("resourcesTraded"), dict):
            m["resourcesTraded"] = {}
        m.setdefault("resourceAlliesMarket", "n")
        # The original protocol separates owned-item storage (map.store) from
        # received gifts (privateState.gifts).  Keep this as an object even for
        # starter saves that contain null, otherwise a stored building reloads
        # as a gift and incorrectly awards its construction XP when re-placed.
        raw_store = m.get("store")
        normalized_store = {}
        if isinstance(raw_store, dict):
            store_values = raw_store.items()
        elif isinstance(raw_store, list):
            store_values = enumerate(raw_store)
        else:
            store_values = ()
        for raw_id, raw_count in store_values:
            try:
                item_id, count = int(raw_id), int(raw_count)
            except (TypeError, ValueError):
                continue
            if item_id >= 0 and count > 0:
                normalized_store[str(item_id)] = count
        m["store"] = normalized_store
        # Unit Warehouse state is per-town. Older/new saves omitted these
        # fields, which made the client show zero slots and forget stored units
        # after refresh.
        m.setdefault("warehouseAditionalCapacitySingle", 0)
        m.setdefault("warehousedUnits", {})
        if (
            int(m.get("warehouseAditionalCapacitySingle", 0) or 0) < 1
            and any(
                item
                and int(item[0]) == Constant.ID_BUILDING_UNIT_WAREHOUSE
                for item in m.get("items", [])
            )
        ):
            # The building includes its first slot. The 2-cash action buys
            # one *additional* slot, rather than activating a 20-cash shell.
            m["warehouseAditionalCapacitySingle"] = 1
        last_trade = int(m.get("timestampLastTrade", 0) or 0)
        if last_trade and ts_now - last_trade >= 20 * 3600:
            m["numTradesDone"] = 0
            m["resourcesTraded"] = {}
            m["timestampLastTrade"] = 0
    # Item collectibles: the client loads privateState.collections (per
    # collection: [completedFlag, count, count, ...]) and collectionsCompleted.
    # Without them the client's load silently fails (try/catch) and collected
    # items vanish on reload. Seed the structures (NUM_COLLECTIONS = 23, so 24
    # slots) so add_collectable has somewhere to persist.
    pState.setdefault("collectionsCompleted", [])
    colls = pState.setdefault("collections", [])
    while len(colls) < 24:
        colls.append([0])
    _ensure_town_list(save)
    _sync_global_level(save)
    # player
    map_idx = _clamp_map(save, map_number)
    _sync_natural_resource_reload_marker(save, map_idx)
    refresh_enemy_camp_timer(save["maps"][map_idx], ts_now)
    response_pstate = copy.deepcopy(pState)
    # The old Flash JSON reader treats a bare false as a truthy object. Keep
    # Python/save state boolean, but emit this protocol flag as numeric 0/1.
    response_pstate["dartsHasFree"] = 1 if pState.get("dartsHasFree") else 0
    player_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": ts_now,
        "playerInfo": save["playerInfo"],
        "map": save["maps"][map_idx],
        "privateState": response_pstate,
        "neighbors": neighbors(USERID)
    }
    if (
        trees_changed
        or minerals_changed
        or social_state_changed
        or natural_state_changed
    ):
        save_session(USERID)
    return player_info

def get_neighbor_info(userid, map_number):
    save = neighbor_session(userid)
    if save is None:
        return ({"result": "error", "error": "unknown_user"}, 404)
    # Visiting a local save must show the same editable empire name and global
    # level as its owner sees. Previously only the self-load path repaired
    # these fields, leaving visit/player cards stuck on the legacy "Emperor".
    pi = save["playerInfo"]
    names = pi.get("map_names") or []
    default = int(pi.get("default_map", 0) or 0)
    if 0 <= default < len(names) and names[default]:
        pi["name"] = names[default]
    _ensure_town_list(save)
    _sync_global_level(save)
    map_idx = _clamp_map(save, map_number)
    neighbor_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": timestamp_now(),
        "playerInfo": save["playerInfo"],
        "map": save["maps"][map_idx],
        "privateState": save["privateState"],
        "neighbors": neighbors(userid)
    }
    return neighbor_info


def get_public_player_info(userid):
    """Public PvP profile served at get_public_player_info.php.

    PopupPlayerProfile reads these exact keys: name, level, map_names[0]
    (empire), honor_points, country, last_logged_in (unix seconds),
    attacks_won, attacks_lost and pid. The PvP counters live in the target
    player's privateState and are updated by end_attack for both attacker and
    defender, so this profile reflects the latest battle results instead of the
    old empty stub."""
    save = neighbor_session(str(userid))
    if save is None:
        return ({"result": "error", "error": "unknown_user"}, 404)
    pi = save.get("playerInfo", {}) or {}
    ps = save.get("privateState", {}) or {}
    maps = save.get("maps") or [{}]
    default = int(pi.get("default_map", 0) or 0)
    if default < 0 or default >= len(maps):
        default = 0
    town = maps[default]

    def _int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    map_names = pi.get("map_names")
    if not isinstance(map_names, list) or not map_names:
        map_names = [_display_name(save)]
    return {
        "result": "ok",
        "pid": str(userid),
        "name": _display_name(save),
        "level": max(1, int(get_level_from_xp(_int(town.get("xp", 0))))),
        "map_names": map_names,
        "honor_points": _int(ps.get("honor")),
        "attacks_won": _int(ps.get("attacksWon")),
        "attacks_lost": _int(ps.get("attacksLost")),
        "country": str(pi.get("country") or ""),
        "last_logged_in": _int(
            pi.get("last_logged_in") or ps.get("lastLogin") or timestamp_now()
        ),
    }
