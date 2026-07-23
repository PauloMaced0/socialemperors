import copy
import datetime

from sessions import (
    session, neighbor_session, neighbors, refresh_enemy_camp_timer,
    _display_name,
)
from engine import timestamp_now
from constants import Constant
from get_game_config import get_level_from_xp


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


def _sync_natural_resource_reload_marker(save, map_idx):
    """Tell the patched SWF whether wild resources were already generated.

    The client skips its one-time initial resource/animal spawn
    (MapInitializer.spawnInitResources / spawnRemainingResources) when
    arrayAnimals[SUBCATFUNC_RESOURCE_REGEN] is set. A brand-new village ships
    with decorative trees, so the reload guard used to treat it as already
    initialized and suppressed that first spawn - which left the tutorial arrow
    pointing at a tree/goblin that was never created. Withhold the marker until
    the tutorial is complete so the first spawn runs during the tutorial (as
    before), then apply the reload guard normally for established towns."""
    animals = save["privateState"].setdefault("arrayAnimals", {})
    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    tutorial_done = int(save["playerInfo"].get("completed_tutorial", 0) or 0)
    initialized = int(save["maps"][map_idx].get("naturalResourcesInitialized", 0) or 0)
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
    _refresh_daily_animal_budget(save, ts_now)
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
