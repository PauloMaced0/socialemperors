from sessions import session, neighbor_session, neighbors
from engine import timestamp_now

DARTS_FREE_INTERVAL = 86400  # daily darts: free game refreshes every 24h

def get_player_info(USERID):
    # Update last logged in
    ts_now = timestamp_now()
    save = session(USERID)
    save["playerInfo"]["last_logged_in"] = ts_now
    # Recompute the daily-darts free availability from the last actual throw
    # so a reload can't offer another game: available only once 24h has passed
    # since the last dart was thrown (the throw is the source of truth,
    # enforced server-side in command.py CMD_DARTS_SHOOT_BALLOON).
    pState = save["privateState"]
    # Migration: saves predating timeStampLastDart approximate the last throw
    # from the last free-claim so an existing same-day player isn't handed a
    # bonus throw. Seed it once; the throw handler owns it thereafter.
    if "timeStampLastDart" not in pState:
        pState["timeStampLastDart"] = int(pState.get("timeStampDartsNewFree", 0) or 0)
    last_dart = int(pState.get("timeStampLastDart", 0) or 0)
    pState["dartsHasFree"] = (ts_now - last_dart) >= DARTS_FREE_INTERVAL
    # player
    player_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": ts_now,
        "playerInfo": session(USERID)["playerInfo"],
        "map": session(USERID)["maps"][0],
        "privateState": session(USERID)["privateState"],
        "neighbors": neighbors(USERID)
    }
    return player_info

def get_neighbor_info(userid, map_number):
    neighbor_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": timestamp_now(),
        "playerInfo": neighbor_session(userid)["playerInfo"],
        "map": neighbor_session(userid)["maps"][map_number],
        "privateState": neighbor_session(userid)["privateState"],
        "neighbors": neighbors(userid)
    }
    return neighbor_info
