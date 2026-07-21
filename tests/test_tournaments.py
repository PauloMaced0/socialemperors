"""Tournament service tests: the tournaments/*.php endpoints must answer
payloads the client's TournamentManager can consume (no more 404 polling).

    /path/to/.venv/bin/python tests/test_tournaments.py

Isolated temp saves dir (patched BEFORE server import, which loads saves).
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions

UID = "test-tourn-0001"
_TMP = tempfile.mkdtemp(prefix="se_tournaments_")


def _make_save(uid):
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = uid
    save["playerInfo"]["cash"] = 100
    save["maps"][0]["coins"] = 10000
    json.dump(save, open(os.path.join(_TMP, f"{uid}.save.json"), "w"), indent=4)


_make_save(UID)
sessions.SAVES_DIR = _TMP

import server  # noqa: E402  (loads villages from the patched SAVES_DIR)

server.app.secret_key = "test-secret"
server.app.testing = True

API = "/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires"
COMMON = {"USERID": UID, "user_key": "k", "language": "en"}


def _client(logged_in_as=None):
    c = server.app.test_client()
    if logged_in_as:
        with c.session_transaction() as s:
            s["USERID"] = logged_in_as
            s["GAMEVERSION"] = "x"
    return c


def _service_payload(obj):
    # ServiceManager posts data as "<hash>;<json>"
    return dict(COMMON, data="0" * 64 + ";" + json.dumps(obj))


def test_get_tournament_info_shape():
    c = _client()
    r = c.post(API + "/tournaments/get_tournament_info.php",
               data=_service_payload({"user_id": UID}))
    assert r.status_code == 200, f"get_tournament_info: {r.status_code}"
    data = r.get_json()["data"]
    # Not in a tournament: the list popup must open.
    assert "tournament" not in data, "player wrongly reported inside a tournament"
    assert data["tournament_friends"] == {}
    # Weekly type 8 must be present with the flags/countdown the thumb reads.
    weekly = data["tournament_weekly"]
    assert "8" in weekly, f"weekly tournament entry missing: {weekly}"
    assert weekly["8"]["open"] == "0" and weekly["8"]["full"] == "0"
    assert 0 < weekly["8"]["timeLeft"] <= 7 * 24 * 3600
    # Every tournament type needs a prize index or the popup crashes on
    # prize[""]["u"] while rendering the reward.
    from get_game_config import get_game_config
    types = get_game_config()["tournament_type"]
    for type_id, definition in types.items():
        idx = data["tournament_reward_id"].get(str(type_id))
        assert idx is not None, f"no reward index for tournament type {type_id}"
        assert 0 <= int(idx) < len(definition["prize"]), f"reward index {idx} out of range"


def test_join_answers_nok_with_refund():
    c = _client()
    for endpoint in ("join_tournament", "create_tournament"):
        r = c.post(API + f"/tournaments/{endpoint}.php",
                   data=_service_payload({"user_id": UID, "tournament_type_id": "2"}))
        assert r.status_code == 200, f"{endpoint}: {r.status_code}"
        data = r.get_json()["data"]
        assert data["result"] == "NOK"
        # evalRefundTournament needs these to give the entry fee back.
        assert data["resources"]["refund"] == 1
        assert data["resources"]["tournament_type_id"] == "2"


def test_leave_cancel_clean_answer_ok():
    c = _client()
    for endpoint in ("leave_tournament", "cancel_tournament", "clean_tournament"):
        r = c.post(API + f"/tournaments/{endpoint}.php",
                   data=_service_payload({"user_id": UID}))
        assert r.status_code == 200, f"{endpoint}: {r.status_code}"
        assert r.get_json()["data"]["result"] == "OK"


def test_fee_and_refund_commands_cancel_out():
    # Client subtracts the fee on join and refunds on NOK; the save must end
    # where it started, for both gold and cash fees.
    import command as _cmd
    v = sessions.session(UID)
    coins0, cash0 = v["maps"][0]["coins"], v["playerInfo"]["cash"]
    _cmd.do_command(UID, "tournament_substract_resources", ["g", 500, "1", 0])
    assert v["maps"][0]["coins"] == coins0 - 500, "gold fee not applied"
    _cmd.do_command(UID, "tournament_refund_resources", ["g", 500])
    assert v["maps"][0]["coins"] == coins0, "gold refund did not restore the fee"
    _cmd.do_command(UID, "tournament_substract_resources", ["c", 15, "2", 0])
    assert v["playerInfo"]["cash"] == cash0 - 15, "cash fee not applied"
    _cmd.do_command(UID, "tournament_refund_resources", ["c", 15])
    assert v["playerInfo"]["cash"] == cash0, "cash refund did not restore the fee"


TESTS = [
    test_get_tournament_info_shape,
    test_join_answers_nok_with_refund,
    test_leave_cancel_clean_answer_ok,
    test_fee_and_refund_commands_cancel_out,
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


