"""Focused Tournament Arena protocol, scheduling and persistence tests.

Run from the repository root:

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/test_tournaments.py
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions


UID = "test-tourn-0001"
_TMP = tempfile.mkdtemp(prefix="se_tournaments_")


def _make_save(uid):
    with open(os.path.join("villages", "initial.json")) as source:
        save = json.load(source)
    save["playerInfo"].update({"pid": uid, "cash": 1000})
    save["playerInfo"]["map_names"][0] = "Arena Tester"
    save["maps"][0].update({"coins": 100000, "xp": 50000})
    with open(os.path.join(_TMP, f"{uid}.save.json"), "w") as target:
        json.dump(save, target, indent=4)


_make_save(UID)
sessions.SAVES_DIR = _TMP

import server  # noqa: E402  (loads villages from the patched SAVES_DIR)
import tournaments  # noqa: E402
from get_game_config import get_game_config  # noqa: E402
from tools.patch_tournament_arena_swf import DEFAULT_SWF, patched  # noqa: E402

server.app.secret_key = "test-secret"
server.app.testing = True

API = "/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires"
COMMON = {"USERID": UID, "user_key": "k", "language": "en"}
TEAM = [662] * 20


def _client():
    return server.app.test_client()


def _service_payload(obj):
    return dict(COMMON, data="0" * 64 + ";" + json.dumps(obj))


def _post(endpoint, obj):
    response = _client().post(
        API + f"/tournaments/{endpoint}.php",
        data=_service_payload(obj),
    )
    assert response.status_code == 200, f"{endpoint}: {response.status_code}"
    return response.get_json()["data"]


def _reset_tournament_state():
    save = sessions.session(UID)
    save["privateState"].pop("activeTournament", None)
    save["privateState"]["tournamentHistory"] = {}
    save["maps"][0]["store"] = {}
    save["maps"][0]["coins"] = 100000
    save["playerInfo"]["cash"] = 1000
    sessions.save_session(UID)


def _today_type_id():
    return str(datetime.datetime.now().astimezone().weekday() + 1)


def _join(type_id=None):
    if type_id is None:
        type_id = _today_type_id()
    return _post("join_tournament", {
        "user_id": UID,
        "tournament_type_id": str(type_id),
        "team": TEAM,
    })


def _finish_all(tournament, points):
    tournament_id = tournament["tournament_id"]
    victims = [match["victim_id"] for match in tournament["players"][0]["matches"]]
    for victim in victims:
        started = _post("start_tournament_match", {
            "user_id": UID,
            "victim_id": victim,
            "tournament_id": tournament_id,
        })
        assert started["result"] == "OK"
        finished = _post("finish_tournament_match", {
            "user_id": UID,
            "victim_id": victim,
            "attacker_won": True,
            "attacker_points": points,
            "victim_points": 0,
            "tournament_id": tournament_id,
        })
        assert finished["result"] == "OK"


def test_schedule_shape_and_daily_rotation():
    _reset_tournament_state()
    data = _post("get_tournament_info", {"user_id": UID})
    assert "tournament" not in data
    assert data["tournament_friends"] == {}
    assert set(data["tournament_daily"]) == set(str(i) for i in range(1, 8))
    open_daily = [key for key, value in data["tournament_daily"].items()
                  if value["open"] == "1"]
    assert open_daily == [_today_type_id()], open_daily
    for type_id, value in data["tournament_daily"].items():
        assert value["day"] in tournaments._DAILY_NAMES
        assert value["full"] == "0" and value["timeLeft"] > 0
    weekly = data["tournament_weekly"]
    assert weekly["8"]["open"] == "1" and weekly["8"]["full"] == "0"
    assert 0 < weekly["8"]["timeLeft"] <= 7 * 24 * 3600

    local_zone = datetime.datetime.now().astimezone().tzinfo
    monday = datetime.datetime(2026, 8, 3, 12, tzinfo=local_zone)
    fixed = tournaments.get_tournament_info(UID, monday)["tournament_daily"]
    assert fixed["1"]["open"] == "1"
    assert fixed["2"]["open"] == "0" and fixed["2"]["day"] == "Tuesday"
    assert fixed["1"]["timeLeft"] == 12 * 3600
    assert fixed["2"]["timeLeft"] == 12 * 3600


def test_tournament_economy_rebalance_and_weekly_fairness_config():
    types = get_game_config()["tournament_type"]
    assert types["2"]["cost"] == 15
    assert all(prize["c"] == 30 for prize in types["2"]["prize"]), \
        "Advanced must remain unchanged"
    assert types["4"]["cost"] == 15
    assert all(prize["c"] == 30 for prize in types["4"]["prize"])
    assert types["6"]["cost"] == 25
    assert all(prize["c"] == 50 for prize in types["6"]["prize"])
    assert len(types["8"]["prize"]) == 1, \
        "Weekly Gold must advertise only its first-place dragon"


def test_closed_daily_type_refunds_instead_of_creating_room():
    _reset_tournament_state()
    closed = str((int(_today_type_id()) % 7) + 1)
    data = _join(closed)
    assert data["result"] == "NOK"
    assert data["resources"] == {
        "refund": 1, "tournament_type_id": closed,
    }
    assert "activeTournament" not in sessions.session(UID)["privateState"]


def test_join_creates_ready_four_player_room_and_survives_restart():
    _reset_tournament_state()
    data = _join()
    tournament = data["tournament"]
    assert len(tournament["players"]) == 4
    assert tournament["players"][0]["user_id"] == UID
    assert tournament["players"][0]["team"] == TEAM
    assert len(tournament["players"][0]["matches"]) == 3
    assert tournament["date_ready"] is not None
    assert tournament["date_finished"] is None
    tournament_id = tournament["tournament_id"]

    # The bracket is save-backed, not an in-memory Flask room.
    sessions.load_saved_villages()
    restored = tournaments.get_tournament_info(UID)["tournament"]
    assert restored["tournament_id"] == tournament_id
    assert len(restored["players"]) == 4


def test_every_tournament_type_builds_and_reopens_its_bracket():
    zone = datetime.datetime.now().astimezone().tzinfo
    monday = datetime.datetime(2026, 8, 3, 12, tzinfo=zone)
    cases = [
        (str(index + 1), monday + datetime.timedelta(days=index))
        for index in range(7)
    ] + [("8", monday)]
    for type_id, now in cases:
        _reset_tournament_state()
        joined = tournaments.join_tournament(UID, type_id, TEAM, now)
        tournament = joined.get("tournament")
        assert tournament is not None, f"type {type_id} did not open"
        assert tournament["tournament_type_id"] == type_id
        assert len(tournament["players"]) == 4
        assert all(
            len(player.get("team", [])) == 20
            for player in tournament["players"]
        )
        tournament_id = tournament["tournament_id"]

        sessions.load_saved_villages()
        reopened = tournaments.get_tournament_info(UID, now)["tournament"]
        assert reopened["tournament_id"] == tournament_id, \
            f"type {type_id} did not survive reopen"
        assert len(reopened["players"]) == 4


def test_match_lifecycle_is_idempotent_and_credits_one_daily_prize():
    _reset_tournament_state()
    joined = _join()
    tournament = joined["tournament"]
    type_id = tournament["tournament_type_id"]
    definition = get_game_config()["tournament_type"][type_id]
    prize = definition["prize"][int(tournament["reward_id"])]
    before_coins = sessions.session(UID)["maps"][0]["coins"]
    before_cash = sessions.session(UID)["playerInfo"]["cash"]

    _finish_all(tournament, 100)
    state = tournaments.get_tournament_info(UID)["tournament"]
    assert state["date_finished"] is not None
    assert state["ranking"]["user"]["rank"] == "1"
    assert state["reward_credited"] is True
    save = sessions.session(UID)
    assert save["maps"][0]["coins"] == before_coins + int(prize.get("g", 0))
    assert save["playerInfo"]["cash"] == before_cash + int(prize.get("c", 0))
    for unit_id, count in prize.get("u", {}).items():
        assert save["maps"][0]["store"][str(unit_id)] == int(count)

    # A retried finish request cannot duplicate the credited resource or unit.
    last = tournament["players"][0]["matches"][-1]
    snapshot = json.dumps({
        "coins": save["maps"][0]["coins"],
        "cash": save["playerInfo"]["cash"],
        "store": save["maps"][0]["store"],
    }, sort_keys=True)
    result = _post("finish_tournament_match", {
        "user_id": UID,
        "victim_id": last["victim_id"],
        "attacker_won": True,
        "attacker_points": 100,
        "tournament_id": tournament["tournament_id"],
    })
    assert result["result"] == "OK"
    save = sessions.session(UID)
    assert snapshot == json.dumps({
        "coins": save["maps"][0]["coins"],
        "cash": save["playerInfo"]["cash"],
        "store": save["maps"][0]["store"],
    }, sort_keys=True)

    assert _post("clean_tournament", {"user_id": UID})["result"] == "OK"
    assert "activeTournament" not in sessions.session(UID)["privateState"]
    replay = _join(type_id)
    assert replay["result"] == "NOK", "same daily slot was replayable"


def test_weekly_gold_requires_first_place_against_configured_bots():
    _reset_tournament_state()
    joined = _join("8")
    tournament = joined["tournament"]
    before = json.dumps(sessions.session(UID)["maps"][0]["store"], sort_keys=True)
    _finish_all(tournament, 70)  # 210 total; Bot 1 has 225.
    state = tournaments.get_tournament_info(UID)["tournament"]
    assert state["ranking"]["user"]["rank"] != "1"
    assert state["reward_credited"] is False
    assert before == json.dumps(sessions.session(UID)["maps"][0]["store"], sort_keys=True)

    assert _post("clean_tournament", {"user_id": UID})["result"] == "OK"
    replay = _join("8")
    assert replay["result"] == "NOK", "Weekly Gold was replayable in one ISO week"

    # A fresh-slot simulation confirms the threshold is achievable and that
    # only the configured first-place dragon is credited.
    _reset_tournament_state()
    winning = _join("8")["tournament"]
    _finish_all(winning, 80)  # 240 total beats Bot 1's 225.
    won = tournaments.get_tournament_info(UID)["tournament"]
    assert won["ranking"]["user"]["rank"] == "1"
    assert won["reward_credited"] is True
    weekly_prize = get_game_config()["tournament_type"]["8"]["prize"][0]
    for unit_id, count in weekly_prize.get("u", {}).items():
        assert sessions.session(UID)["maps"][0]["store"][str(unit_id)] == int(count)


def test_leave_and_clean_validate_lifecycle():
    _reset_tournament_state()
    tournament = _join()["tournament"]
    assert _post("clean_tournament", {"user_id": UID})["result"] == "NOK"
    assert tournaments.get_tournament_info(UID)["tournament"]["tournament_id"] \
        == tournament["tournament_id"]
    assert _post("leave_tournament", {"user_id": UID})["result"] == "OK"
    assert "activeTournament" not in sessions.session(UID)["privateState"]


def test_fee_and_refund_commands_cancel_out():
    import command as game_command
    _reset_tournament_state()
    village = sessions.session(UID)
    coins0 = village["maps"][0]["coins"]
    cash0 = village["playerInfo"]["cash"]
    game_command.do_command(UID, "tournament_substract_resources", ["g", 500, "1", 0])
    game_command.do_command(UID, "tournament_refund_resources", ["g", 500])
    game_command.do_command(UID, "tournament_substract_resources", ["c", 15, "2", 0])
    game_command.do_command(UID, "tournament_refund_resources", ["c", 15])
    assert village["maps"][0]["coins"] == coins0
    assert village["playerInfo"]["cash"] == cash0


def test_selected_tournament_team_survives_reload():
    import command as game_command
    _reset_tournament_state()
    packet = {
        "ts": 1,
        "first_number": 1,
        "accessToken": "x",
        "tries": 0,
        "publishActions": [],
        "commands": [{
            "cmd": "set_attack_team",
            "args": ["tournament", json.dumps(TEAM)],
        }],
    }
    game_command.command(UID, packet)
    sessions.load_saved_villages()
    assert sessions.session(UID)["privateState"]["teams"]["tournament"] \
        == TEAM


def test_bundled_client_has_daily_and_first_place_patch():
    assert patched(DEFAULT_SWF.read_bytes()), (
        "Tournament cards still expose every type every day, or Weekly Gold "
        "still treats the top ten as winners"
    )


TESTS = [
    test_schedule_shape_and_daily_rotation,
    test_tournament_economy_rebalance_and_weekly_fairness_config,
    test_closed_daily_type_refunds_instead_of_creating_room,
    test_join_creates_ready_four_player_room_and_survives_restart,
    test_every_tournament_type_builds_and_reopens_its_bracket,
    test_match_lifecycle_is_idempotent_and_credits_one_daily_prize,
    test_weekly_gold_requires_first_place_against_configured_bots,
    test_leave_and_clean_validate_lifecycle,
    test_fee_and_refund_commands_cancel_out,
    test_selected_tournament_team_survives_reload,
    test_bundled_client_has_daily_and_first_place_patch,
]


def main():
    passed = failed = 0
    try:
        for test in TESTS:
            try:
                test()
                print(f"PASS  {test.__name__}")
                passed += 1
            except Exception as exc:
                print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
