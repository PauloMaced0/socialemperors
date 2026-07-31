"""Route auth tests: game endpoints must act on the logged-in session's
village, never on the client-posted USERID.

    /path/to/.venv/bin/python tests/test_routes.py

Isolated temp saves dir (patched BEFORE server import, which loads saves).
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions

UID = "test-route-0001"
OTHER = "test-route-9999"
_TMP = tempfile.mkdtemp(prefix="se_routes_")


def _make_save(uid, name):
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = uid
    save["playerInfo"]["map_names"] = [name]
    save["maps"][0]["coins"] = 0
    json.dump(save, open(os.path.join(_TMP, f"{uid}.save.json"), "w"), indent=4)


_make_save(UID, "Route Test Empire")
_make_save(OTHER, "Route Rival")
sessions.SAVES_DIR = _TMP

import server  # noqa: E402  (loads villages from the patched SAVES_DIR)

server.app.secret_key = "test-secret"
server.app.testing = True

API = "/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires"


def _client(logged_in_as=None):
    c = server.app.test_client()
    if logged_in_as:
        with c.session_transaction() as s:
            s["USERID"] = logged_in_as
            s["GAMEVERSION"] = "x"
    return c


def _cmd_payload(cmds):
    data = json.dumps({"ts": 0, "first_number": 1, "accessToken": "x",
                       "tries": 1, "publishActions": [], "commands": cmds})
    return {"USERID": OTHER, "user_key": "k", "language": "en",
            "client_id": "1", "data": "0" * 64 + ";" + data}


def test_command_requires_login():
    c = _client()
    r = c.post(API + "/command.php", data=_cmd_payload([]))
    assert r.status_code == 403, f"anonymous command accepted: {r.status_code}"


def test_command_ignores_posted_userid():
    c = _client(logged_in_as=UID)
    # posted USERID says OTHER; reward must land on UID's village
    r = c.post(API + "/command.php", data=_cmd_payload(
        [{"cmd": "win_bonus", "args": [0, 0, 0, 0, 0]}]))  # daily bonus: +250 gold
    assert r.status_code == 200, f"logged-in command rejected: {r.status_code}"
    mine = json.load(open(os.path.join(_TMP, f"{UID}.save.json")))
    other = json.load(open(os.path.join(_TMP, f"{OTHER}.save.json")))
    assert int(other["maps"][0]["coins"]) == 0, "command applied to the POSTED USERID (spoofable)"
    assert int(mine["maps"][0]["coins"]) > 0, "command not applied to the session village"


def test_get_player_info_requires_login():
    c = _client()
    r = c.post(API + "/get_player_info.php",
               data={"USERID": OTHER, "user_key": "k", "language": "en", "client_id": "1"})
    assert r.status_code == 403, f"anonymous player info served: {r.status_code}"


def test_get_player_info_serves_session_village():
    c = _client(logged_in_as=UID)
    r = c.post(API + "/get_player_info.php",
               data={"USERID": OTHER, "user_key": "k", "language": "en", "client_id": "1"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["playerInfo"]["pid"] == UID, "player info served for the POSTED USERID (spoofable)"


def test_ship_map_route_requires_completed_harbour_staffing():
    from constants import Constant
    from get_game_config import get_game_config

    save = sessions.session(UID)
    town = save["maps"][0]
    town["items"] = [
        item for item in town["items"]
        if int(item[0]) not in (
            Constant.ID_BUILDING_DOCK,
            Constant.ID_BUILDING_TROLL_HARBOUR,
        )
    ]
    harbour = [
        Constant.ID_BUILDING_DOCK,
        68, 68, 0, 1, 0, [], {"si": []},
    ]
    town["items"].append(harbour)
    quest_id = str(get_game_config()["globals"]["ISLE_ORDER"][0])
    payload = {
        "USERID": UID,
        "user": quest_id,
        "map": "0",
        "user_key": "k",
        "language": "en",
        "client_id": "1",
    }
    c = _client(logged_in_as=UID)

    locked = c.post(API + "/get_player_info.php", data=payload)
    assert locked.status_code == 403
    assert locked.get_json()["error"] == "harbour_staffing_required"

    harbour[7]["si"] = None
    opened = c.post(API + "/get_player_info.php", data=payload)
    assert opened.status_code == 200, \
        "completed Harbor did not unlock its Ship Land map"


def test_player_info_includes_town_list():
    # The client feeds privateState.maps to TownManager.init(); a missing
    # field crashes hasSecondTown() (null.length, AVM2 #1009) when the Town /
    # race-selector popup opens, leaving the screen dimmed. One entry per
    # owned town, each carrying its race code.
    c = _client(logged_in_as=UID)
    r = c.post(API + "/get_player_info.php",
               data={"USERID": UID, "user_key": "k", "language": "en", "client_id": "1"})
    assert r.status_code == 200
    maps = r.get_json()["privateState"].get("maps")
    assert isinstance(maps, list) and len(maps) >= 1, f"privateState.maps missing/empty: {maps}"
    assert all("r" in t for t in maps), f"town entries lack race code 'r': {maps}"
    # single-town save -> hasSecondTown() is false, opens the buy-town popup
    assert len(maps) == 1, f"expected one town for the test save, got {len(maps)}"


def test_switch_to_own_second_town_no_500():
    # Buying a second town then travelling to it sends get_player_info with
    # user == own id and map=1. It must serve the own save's map 1 (not the
    # neighbour path, which 500'd), so the game doesn't hang on the loading
    # bar. Uses UID; give it a second town first via buy_map.
    c = _client(logged_in_as=UID)
    import sessions as _s
    v = _s.session(UID)
    v["maps"][0]["level"] = 50
    v["maps"][0]["coins"] = 200000
    if len(v["maps"]) < 2:
        import command as _cmd
        _cmd.do_command(UID, "buy_map", [1, 0, "t", 0])
    r = c.post(API + "/get_player_info.php",
               data={"USERID": UID, "user": UID, "map": "1",
                     "user_key": "k", "language": "en", "client_id": "1"})
    assert r.status_code == 200, f"loading own second town 500'd: {r.status_code}"
    body = r.get_json()
    assert body["result"] == "ok"
    assert body["map"]["race"] == "t", "second town not served"
    assert len(body["privateState"]["maps"]) == 2, "town list not updated to 2"


def test_second_town_shows_global_level():
    # Player level is global: the second town must report the account level,
    # not its own fresh xp, so switching towns never drops the level.
    import sessions as _s
    import command as _cmd
    v = _s.session(UID)
    v["maps"][0]["level"] = 99
    v["maps"][0]["xp"] = 9497483
    v["maps"][0]["coins"] = 200000
    if len(v["maps"]) < 2:
        _cmd.do_command(UID, "buy_map", [1, 0, "t", 0])
    # force the second town to a stale low level; serving must override it
    v["maps"][1]["level"] = 3
    v["maps"][1]["xp"] = 92
    c = _client(logged_in_as=UID)
    r = c.post(API + "/get_player_info.php",
               data={"USERID": UID, "user": UID, "map": "1",
                     "user_key": "k", "language": "en", "client_id": "1"})
    assert r.get_json()["map"]["level"] == 99, "second town did not show global level 99"
    # main town must be untouched
    assert v["maps"][0]["level"] == 99, "main town level changed"


def test_own_town_out_of_range_falls_back():
    c = _client(logged_in_as=OTHER)  # OTHER has one town
    r = c.post(API + "/get_player_info.php",
               data={"USERID": OTHER, "user": OTHER, "map": "1",
                     "user_key": "k", "language": "en", "client_id": "1"})
    assert r.status_code == 200, "out-of-range own map 500'd instead of falling back"
    assert r.get_json()["result"] == "ok"


def test_other_saves_are_pvp_opponents_not_automatic_neighbors():
    c = _client(logged_in_as=UID)
    player = c.post(
        API + "/get_player_info.php",
        data={
            "USERID": UID, "user_key": "k", "language": "en",
            "client_id": "1",
        },
    ).get_json()
    assert not any(
        str(entry["pid"]) == OTHER for entry in player["neighbors"]
    ), "unrelated save leaked into the social neighbour list"

    rival_level = sessions.save_info(OTHER)["level"]
    r = c.get(
        API + "/get_continent_ranking.php",
        query_string={
            "USERID": OTHER, "worldChange": "0", "map": "0",
            "user_key": "k", "level_id": str(rival_level),
        },
    )
    assert r.status_code == 200
    body = r.get_json()
    ids = {str(entry["user_id"]) for entry in body["continent"]}
    assert OTHER in ids, "PvP matchmaking omitted the rival"
    assert all(
        entry.get("name") and entry.get("race") and entry.get("level")
        and entry.get("nivel") == rival_level
        for entry in body["continent"]
    ), f"PvP continent still contains blank/stub players: {body}"
    other_island = c.get(
        API + "/get_continent_ranking.php",
        query_string={"USERID": UID, "user_key": "k", "level_id": "50"},
    ).get_json()["continent"]
    assert not any(str(entry["user_id"]) == OTHER for entry in other_island), \
        "the same opponent was replicated on an unrelated PvP island"
    own_level = min(50, sessions.save_info(UID)["level"])
    home = c.get(
        API + "/get_continent_ranking.php",
        query_string={
            "USERID": UID, "user_key": "k", "level_id": str(own_level),
        },
    ).get_json()["continent"]
    assert any(str(entry["user_id"]) == UID for entry in home), \
        "home PvP continent omitted the client's required own slot"
    empty = c.get(
        API + "/get_continent_ranking.php",
        query_string={"USERID": UID, "user_key": "k", "level_id": "50"},
    ).get_json()
    assert empty["level_id"] == 50, \
        "empty PvP continent omitted the level needed by the client fallback"


def test_friends_page_links_and_unlinks_real_players_reciprocally():
    c = _client(logged_in_as=UID)
    page = c.get("/friends")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Route Rival" in html and "Add friend" in html

    sessions.unlink_friend(UID, OTHER)
    sessions.session(UID)["privateState"]["friendRequests"] = []
    sessions.session(OTHER)["privateState"]["friendRequests"] = []

    # Sending a request does not link yet; the target must accept.
    sent = c.post("/friends", data={"action": "request", "friend_id": OTHER})
    assert sent.status_code == 200
    assert "Friend request sent." in sent.get_data(as_text=True)
    assert not sessions.is_friend(UID, OTHER), "request linked without acceptance"

    accepted = _client(logged_in_as=OTHER).post(
        "/friends", data={"action": "accept", "friend_id": UID},
    )
    assert accepted.status_code == 200
    assert "Friend request accepted." in accepted.get_data(as_text=True)
    assert sessions.is_friend(UID, OTHER)
    assert sessions.is_friend(OTHER, UID), \
        "Accepting created a one-way relationship"
    game_html = c.get("/ruffle.html").get_data(as_text=True)
    assert "Route Rival" in game_html, \
        "linked friend did not reach the Ruffle friendsInfo payload"

    removed = c.post(
        "/friends",
        data={"action": "remove", "friend_id": OTHER},
    )
    assert removed.status_code == 200
    assert "Friend removed." in removed.get_data(as_text=True)
    assert not sessions.is_friend(UID, OTHER)
    assert not sessions.is_friend(OTHER, UID)


def test_pvp_continent_requires_login():
    r = _client().get(
        API + "/get_continent_ranking.php",
        query_string={
            "USERID": UID, "worldChange": "0", "map": "0", "user_key": "k",
        },
    )
    assert r.status_code == 403


def test_player_cards_and_visits_use_the_saved_empire_name():
    rival = sessions.session(OTHER)
    rival["playerInfo"]["map_names"][0] = "Rival Kingdom"
    rival["playerInfo"]["name"] = "Emperor"
    sessions.save_session(OTHER)
    c = _client(logged_in_as=UID)

    visit = c.post(
        API + "/get_player_info.php",
        data={
            "USERID": UID, "user": OTHER, "map": "0",
            "user_key": "k", "language": "en", "client_id": "1",
        },
    )
    assert visit.status_code == 200
    assert visit.get_json()["playerInfo"]["name"] == "Rival Kingdom"

    ranking = c.get(
        API + "/get_continent_ranking.php",
        query_string={
            "USERID": UID, "user_key": "k",
            "level_id": str(sessions.save_info(OTHER)["level"]),
        },
    ).get_json()["continent"]
    card = next(entry for entry in ranking if str(entry["user_id"]) == OTHER)
    assert card["name"] == "Rival Kingdom"


def test_level_14_arthur_village_finishes_loading():
    c = _client(logged_in_as=UID)
    r = c.post(
        API + "/get_player_info.php",
        data={
            "USERID": UID, "user": "100000031", "map": "0",
            "user_key": "k", "language": "en", "client_id": "1",
        },
    )
    assert r.status_code == 200, "Arthur's level 11-20 village failed to load"
    body = r.get_json()
    assert body["result"] == "ok"
    assert body["playerInfo"]["name"] and isinstance(body["map"]["items"], list)
    # A 200 alone did not prove the visit works: Arthur's bundled save omits the
    # privateState/map fields the client reads on load, and their absence makes
    # the loader silently loop 0->100 forever. The visit response must be seeded
    # the same way a self-load is.
    ps = body["privateState"]
    assert isinstance(ps.get("collections"), list) and len(ps["collections"]) >= 24, \
        "neighbor visit not seeded with collections -> client loading bar loops"
    assert ps["collections"][0] == []
    assert isinstance(ps.get("templeStep"), list)
    assert isinstance(ps.get("attacksReceived"), list)
    assert ps.get("dartsHasFree") in (0, 1), "dartsHasFree must be numeric 0/1"
    m = body["map"]
    assert "warehousedUnits" in m and "numTradesDone" in m


def test_ruffle_page_uses_current_origin_and_supported_autoplay():
    c = _client(logged_in_as=UID)
    # Keep the host used by Flask's test-session cookie; the non-default port
    # is what proves the template no longer assumes :5050.
    r = c.get("/ruffle.html", base_url="http://localhost:5099")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert (
        "http://localhost:5099/default01.static.socialpointgames.com/"
        "static/socialempires/flash/x" in html
    ), "Ruffle game URL still assumes port 5050"
    assert "flash/SELoader.swf?build=20260723" in html
    assert 'autoplay: "on"' in html
    assert "autoplay: true" not in html
    assert 'unmuteOverlay: "hidden"' in html, "Click-to-unmute overlay not suppressed"
    assert 'swftoload: "http://localhost:5099/' in html
    assert "static/socialempires/flash/x?build=" in html
    assert 'staticUrl: "http://localhost:5099/' in html
    assert 'dynamicUrl: "http://localhost:5099/' in html


def test_nginx_forwarded_origin_needs_no_body_substitution():
    c = server.app.test_client()
    public_origin = "http://social-empires.local"
    with c.session_transaction(base_url=public_origin) as s:
        s["USERID"] = UID
        s["GAMEVERSION"] = "x"
    r = c.get(
        "/ruffle.html",
        base_url=public_origin,
        headers={
            "X-Forwarded-For": "192.0.2.10",
            "X-Forwarded-Proto": "https",
        },
    )
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'swftoload: "https://social-empires.local/' in html
    assert "static/socialempires/flash/x?build=" in html
    assert 'staticUrl: "https://social-empires.local/' in html
    assert 'dynamicUrl: "https://social-empires.local/' in html
    assert "127.0.0.1:5050" not in html
    assert "localhost:5050" not in html


ASSETS = "/default01.static.socialpointgames.com/static/socialempires"


def test_missing_asset_returns_404_not_500():
    # A missing projectile/effect SWF must 404, never 500. Combat fires these
    # every shot; a 500 logs a traceback and makes Ruffle error each frame,
    # tanking framerate during a fight.
    c = server.app.test_client()
    r = c.get(f"{ASSETS}/fx/p.definitely_missing_asset_xyz.swf")
    assert r.status_code == 404, f"missing asset returned {r.status_code}, expected 404"


def test_missing_asset_is_cached():
    # Second request for the same missing asset must not repeat the (slow,
    # dead) CDN round-trip.
    c = server.app.test_client()
    path = "fx/p.another_missing_asset_xyz.swf"
    c.get(f"{ASSETS}/{path}")
    assert path in server._missing_assets, "missing asset not cached for fast repeat 404s"
    assert c.get(f"{ASSETS}/{path}").status_code == 404


def test_present_asset_served():
    c = server.app.test_client()
    r = c.get(f"{ASSETS}/fx/p.fireBall.swf")  # exists in assets/fx
    assert r.status_code == 200 and len(r.data) > 0, "bundled asset not served"


def test_mini_fireball_combat_asset_served():
    c = server.app.test_client()
    r = c.get(f"{ASSETS}/fx/p.miniFireball2.swf")
    assert r.status_code == 200, \
        "miniFireball2 combat effect still returns 404"
    assert r.data[:3] in (b"FWS", b"CWS", b"ZWS") and len(r.data) > 100, \
        "miniFireball2 route did not return a valid SWF"


def test_public_player_profile_requires_login():
    c = _client()
    r = c.get(API + "/get_public_player_info.php", query_string={"USERID": OTHER})
    assert r.status_code == 403, f"anonymous profile read accepted: {r.status_code}"


def test_public_player_profile_returns_stats():
    # Give OTHER some persisted PvP stats, then read their public profile as UID.
    other = sessions.session(OTHER)
    other["playerInfo"]["map_names"] = ["Rival Empire"]
    other["privateState"]["honor"] = 42
    other["privateState"]["attacksWon"] = 3
    other["privateState"]["attacksLost"] = 1
    c = _client(logged_in_as=UID)
    r = c.get(API + "/get_public_player_info.php", query_string={"USERID": OTHER})
    assert r.status_code == 200, f"profile read rejected: {r.status_code}"
    body = json.loads(r.data)
    assert body["pid"] == OTHER
    assert body["name"] == "Rival Empire", f"empire name wrong: {body.get('name')}"
    assert body["honor_points"] == 42, "honor_points not reflected"
    assert body["attacks_won"] == 3 and body["attacks_lost"] == 1, "battle stats not reflected"
    assert isinstance(body.get("map_names"), list) and body["map_names"], "map_names missing"


def test_ally_popup_api_sends_request_and_requires_acceptance():
    # The in-game ADD ALLY popup (ruffle.html) uses these JSON routes; the
    # Flash client calls the page's gotoNeighbors hook, so no SWF is involved.
    s = sessions
    s.unlink_friend(UID, OTHER)

    r = server.app.test_client().get("/api/ally_candidates")
    assert r.status_code == 403, "candidates served without a session"
    r = server.app.test_client().post("/api/add_ally", json={"pid": OTHER})
    assert r.status_code == 403, "add_ally accepted without a session"

    c = _client(logged_in_as=UID)
    body = c.get("/api/ally_candidates").get_json()
    row = next(e for e in body["candidates"] if e["userid"] == OTHER)
    assert row["name"] and isinstance(row["level"], int)
    assert not any(e["userid"] == UID for e in body["candidates"]), \
        "own village offered as an ally candidate"

    r = c.post("/api/add_ally", json={"pid": OTHER})
    assert r.status_code == 200 and r.get_json()["result"] == "ok"
    assert not s.is_friend(UID, OTHER), \
        "client-side add ally bypassed the target player's acceptance"
    other = json.load(open(os.path.join(_TMP, f"{OTHER}.save.json")))
    assert UID in [str(v) for v in other["privateState"]["friendRequests"]]

    # Pending players stay visible but are marked requested.
    body = c.get("/api/ally_candidates").get_json()
    pending = next(e for e in body["candidates"] if e["userid"] == OTHER)
    assert pending["requested"] is True

    # Garbage pids are rejected without effect.
    assert c.post("/api/add_ally", json={"pid": "no-such"}).status_code == 404
    assert c.post("/api/add_ally", json={"pid": UID}).status_code == 400
    s.unlink_friend(UID, OTHER)


def test_friend_request_and_accept_flow():
    s = sessions
    s.unlink_friend(UID, OTHER)
    s.session(UID)["privateState"]["friendRequests"] = []
    s.session(OTHER)["privateState"]["friendRequests"] = []
    assert s.request_friend(UID, OTHER) == "requested"
    assert not s.is_friend(UID, OTHER), "request should not link immediately"
    assert any(r["userid"] == UID for r in s.incoming_friend_requests(OTHER)), "request not in target inbox"
    assert not s.incoming_friend_requests(UID), "request leaked into sender inbox"
    assert s.request_friend(UID, OTHER) == "pending", "duplicate request not detected"
    assert s.accept_friend(OTHER, UID) is True
    assert s.is_friend(UID, OTHER) and s.is_friend(OTHER, UID), "accept did not create reciprocal link"
    assert not s.incoming_friend_requests(OTHER), "inbox not cleared after accept"
    s.unlink_friend(UID, OTHER)


def test_friend_request_decline():
    s = sessions
    s.unlink_friend(UID, OTHER)
    s.session(UID)["privateState"]["friendRequests"] = []
    s.session(OTHER)["privateState"]["friendRequests"] = []
    assert s.request_friend(UID, OTHER) == "requested"
    assert s.decline_friend(OTHER, UID) is True
    assert not s.is_friend(UID, OTHER), "decline should not link"
    assert not s.incoming_friend_requests(OTHER), "declined request still in inbox"


def test_mutual_request_auto_accepts():
    s = sessions
    s.unlink_friend(UID, OTHER)
    s.session(UID)["privateState"]["friendRequests"] = []
    s.session(OTHER)["privateState"]["friendRequests"] = []
    assert s.request_friend(UID, OTHER) == "requested"
    # OTHER also requests UID -> the pending request matches and links at once.
    assert s.request_friend(OTHER, UID) == "accepted"
    assert s.is_friend(UID, OTHER) and s.is_friend(OTHER, UID)
    s.unlink_friend(UID, OTHER)


def test_friends_page_renders_with_sections():
    c = _client(logged_in_as=UID)
    r = c.get("/friends")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Friend requests" in html and "All empires" in html, "friends page sections missing"


TESTS = [
    test_command_requires_login,
    test_command_ignores_posted_userid,
    test_get_player_info_requires_login,
    test_get_player_info_serves_session_village,
    test_ship_map_route_requires_completed_harbour_staffing,
    test_player_info_includes_town_list,
    test_switch_to_own_second_town_no_500,
    test_second_town_shows_global_level,
    test_own_town_out_of_range_falls_back,
    test_other_saves_are_pvp_opponents_not_automatic_neighbors,
    test_friends_page_links_and_unlinks_real_players_reciprocally,
    test_pvp_continent_requires_login,
    test_player_cards_and_visits_use_the_saved_empire_name,
    test_level_14_arthur_village_finishes_loading,
    test_ruffle_page_uses_current_origin_and_supported_autoplay,
    test_nginx_forwarded_origin_needs_no_body_substitution,
    test_missing_asset_returns_404_not_500,
    test_missing_asset_is_cached,
    test_present_asset_served,
    test_mini_fireball_combat_asset_served,
    test_public_player_profile_requires_login,
    test_public_player_profile_returns_stats,
    test_ally_popup_api_sends_request_and_requires_acceptance,
    test_friend_request_and_accept_flow,
    test_friend_request_decline,
    test_mutual_request_auto_accepts,
    test_friends_page_renders_with_sections,
]


def main():
    passed = failed = 0
    try:
        for t in TESTS:
            try:
                t()
                print(f"PASS  {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
                failed += 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
