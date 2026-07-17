from sessions import session, neighbor_session, neighbors
from engine import timestamp_now

DARTS_FREE_INTERVAL = 86400  # daily darts: free game refreshes every 24h

def get_player_info(USERID):
    # Update last logged in
    ts_now = timestamp_now()
    save = session(USERID)
    save["playerInfo"]["last_logged_in"] = ts_now
    # Recompute the daily-darts free availability from the last claim so a
    # reload can't hand out another free game: available only once the 24h
    # window since timeStampDartsNewFree has elapsed.
    pState = save["privateState"]
    last_free = int(pState.get("timeStampDartsNewFree", 0) or 0)
    pState["dartsHasFree"] = (ts_now - last_free) >= DARTS_FREE_INTERVAL
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
