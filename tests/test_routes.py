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


def _make_save(uid):
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = uid
    save["maps"][0]["coins"] = 0
    json.dump(save, open(os.path.join(_TMP, f"{uid}.save.json"), "w"), indent=4)


_make_save(UID)
_make_save(OTHER)
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


TESTS = [
    test_command_requires_login,
    test_command_ignores_posted_userid,
    test_get_player_info_requires_login,
    test_get_player_info_serves_session_village,
    test_player_info_includes_town_list,
    test_switch_to_own_second_town_no_500,
    test_second_town_shows_global_level,
    test_own_town_out_of_range_falls_back,
    test_missing_asset_returns_404_not_500,
    test_missing_asset_is_cached,
    test_present_asset_served,
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
