from sessions import session, neighbor_session, neighbors
from engine import timestamp_now


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
    if len(maps) < 2:
        return
    canonical = maps[int(save["playerInfo"].get("default_map", 0) or 0)]
    xp, level = canonical.get("xp", 0), canonical.get("level", 1)
    for i, town in enumerate(maps):
        if town is not canonical:
            town["xp"], town["level"] = xp, level


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
        pState["dartsHasFree"] = 0
    # Emit the flag as numeric 0/1, never a JSON bool: the old Flash client's
    # JSON reader treats a bare `false` as a truthy object, so a consumed free
    # game would still render the free "Play" button and let a reload throw
    # again (charged as paid or rejected). Numeric flags read correctly.
    pState["dartsHasFree"] = 1 if pState.get("dartsHasFree") else 0
    # Supreme Bahamut Temple: ensure the fields the client reads on load exist,
    # so older saves get valid Temple state automatically (the client does
    # Utils.inArray(step, privateState["templeStep"]) and would break on an
    # undefined list). templeStep = completed step indices; timeStampTemple =
    # last-step time that gates the 48h wait.
    pState.setdefault("templeStep", [])
    pState.setdefault("timeStampTemple", 0)
    _ensure_town_list(save)
    _sync_global_level(save)
    # player
    map_idx = _clamp_map(save, map_number)
    player_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": ts_now,
        "playerInfo": save["playerInfo"],
        "map": save["maps"][map_idx],
        "privateState": save["privateState"],
        "neighbors": neighbors(USERID)
    }
    return player_info

def get_neighbor_info(userid, map_number):
    save = neighbor_session(userid)
    if save is None:
        return ({"result": "error", "error": "unknown_user"}, 404)
    _ensure_town_list(save)
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
