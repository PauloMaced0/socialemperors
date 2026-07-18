from sessions import session, neighbor_session, neighbors
from engine import timestamp_now


def _clamp_map(save, map_number):
    """Pick a valid map index for a village. The client requests map 1 when
    the player clicks "Town" even if they own no second town; serving an
    out-of-range index would 500 and, because the client's map-load error
    handler is a no-op, leave the screen stuck behind the loading cover.
    Fall back to the player's default map so the client always gets a valid
    payload and unlocks."""
    maps = save["maps"]
    if map_number is None:
        map_number = int(save["playerInfo"].get("default_map", 0) or 0)
    if map_number < 0 or map_number >= len(maps):
        map_number = int(save["playerInfo"].get("default_map", 0) or 0)
    if map_number < 0 or map_number >= len(maps):
        map_number = 0
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
        pState["dartsHasFree"] = False
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
