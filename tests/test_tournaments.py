"""Focused shared Tournament Arena scheduling and persistence tests.

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


UIDS = [f"test-tourn-{index:04d}" for index in range(1, 7)]
UID = UIDS[0]
_TMP = tempfile.mkdtemp(prefix="se_tournaments_")


def _make_save(uid, index):
    with open(os.path.join("villages", "initial.json")) as source:
        save = json.load(source)
    save["playerInfo"].update({"pid": uid, "cash": 1000})
    save["playerInfo"]["map_names"][0] = f"Arena Tester {index}"
    save["maps"][0].update({"coins": 100000, "xp": 50000})
    with open(os.path.join(_TMP, f"{uid}.save.json"), "w") as target:
        json.dump(save, target, indent=4)


for player_index, player_uid in enumerate(UIDS, 1):
    _make_save(player_uid, player_index)
sessions.SAVES_DIR = _TMP

import server  # noqa: E402  (loads villages from the patched SAVES_DIR)
import tournaments  # noqa: E402
from get_game_config import get_game_config  # noqa: E402
from tools.patch_shared_tournament_swf import (  # noqa: E402
    DEFAULT_SWF as SHARED_SWF,
    patched as shared_patched,
)
from tools.patch_tournament_arena_swf import (  # noqa: E402
    DEFAULT_SWF,
    patched,
)
from tools.patch_tournament_history_swf import (  # noqa: E402
    DEFAULT_SWF as HISTORY_SWF,
    patched as history_patched,
)

server.app.secret_key = "test-secret"
server.app.testing = True

API = "/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires"
COMMON = {"USERID": UID, "user_key": "k", "language": "en"}
TEAM = [662] * 20
ZONE = datetime.datetime.now().astimezone().tzinfo
TYPE1_ADMISSION = datetime.datetime(2026, 8, 3, 12, tzinfo=ZONE)
TYPE1_BATTLE = datetime.datetime(2026, 8, 4, 12, tzinfo=ZONE)
TYPE1_FINISHED = datetime.datetime(2026, 8, 5, 0, 1, tzinfo=ZONE)


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
    state_path = os.path.join(_TMP, tournaments._STATE_FILENAME)
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
    for uid in UIDS:
        save = sessions.session(uid)
        save["privateState"].pop("activeTournament", None)
        save["privateState"].pop("activeTournamentId", None)
        save["privateState"]["tournamentHistory"] = {}
        save["maps"][0]["store"] = {}
        save["maps"][0]["coins"] = 100000
        save["playerInfo"]["cash"] = 1000
        sessions.save_session(uid)


def _join(uid=UID, type_id="1", now=TYPE1_ADMISSION):
    return tournaments.join_tournament(uid, type_id, TEAM, now)


def _player(tournament, uid=UID):
    return next(
        value for value in tournament["players"]
        if str(value["user_id"]) == uid
    )


def _finish_all(tournament, points, uid=UID, now=TYPE1_BATTLE):
    tournament_id = tournament["tournament_id"]
    victims = [match["victim_id"] for match in _player(tournament, uid)["matches"]]
    for offset, victim in enumerate(victims):
        stamp = now + datetime.timedelta(minutes=offset)
        started = tournaments.start_tournament_match(
            uid, tournament_id, victim, stamp,
        )
        assert started["result"] == "OK"
        finished = tournaments.finish_tournament_match(
            uid, tournament_id, victim, True, points,
            stamp + datetime.timedelta(seconds=30),
        )
        assert finished["result"] == "OK"


def test_non_overlapping_two_day_rotation():
    _reset_tournament_state()
    admission = tournaments.get_tournament_info(UID, TYPE1_ADMISSION)
    assert admission["tournament_daily"]["1"]["open"] == "1"
    assert admission["tournament_daily"]["1"]["phase"] == "admission"
    assert admission["tournament_daily"]["1"]["timeLeft"] == 12 * 3600
    assert all(
        value["open"] == "0"
        for key, value in admission["tournament_daily"].items() if key != "1"
    )
    assert admission["tournament_weekly"]["8"]["open"] == "0"

    battle = tournaments.get_tournament_info(UID, TYPE1_BATTLE)
    assert all(value["open"] == "0" for value in battle["tournament_daily"].values())
    assert battle["tournament_daily"]["1"]["phase"] == "battle"
    assert battle["tournament_weekly"]["8"]["open"] == "0"

    next_admission = TYPE1_ADMISSION + datetime.timedelta(days=2)
    following = tournaments.get_tournament_info(UID, next_admission)
    assert following["tournament_daily"]["2"]["open"] == "1"
    assert following["tournament_daily"]["2"]["day"] == "06 Aug"


def test_all_eight_types_take_one_rotation_slot():
    _reset_tournament_state()
    for index, type_id in enumerate(tournaments._ALL_TYPE_IDS):
        now = TYPE1_ADMISSION + datetime.timedelta(days=index * 2)
        state = tournaments.get_tournament_info(UIDS[index % len(UIDS)], now)
        combined = dict(state["tournament_daily"], **state["tournament_weekly"])
        assert [key for key, value in combined.items() if value["open"] == "1"] \
            == [type_id]


def test_config_has_five_slots_four_bots_and_renamed_gold_event():
    config = get_game_config()
    types = config["tournament_type"]
    assert all(int(value["num_players"]) == 5 for value in types.values())
    assert types["8"]["name"] == "GOLD TOURNAMENT"
    assert len(types["8"]["weekly_opponent"]) >= 4
    assert len(types["8"]["prize"]) == 1
    assert types["2"]["cost"] == 15
    assert types["4"]["cost"] == 15
    assert types["6"]["cost"] == 25
    help_text = next(
        value["text"] for value in config["localization_strings"]
        if isinstance(value, dict)
        and value.get("name") == "TOURNAMENT_HELP_TEXT"
    )
    assert "Play against 4 opponents" in help_text


def test_real_players_share_one_admission_room_and_restart_keeps_it():
    _reset_tournament_state()
    first = _join(UIDS[0])["tournament"]
    second = _join(UIDS[1])["tournament"]
    third = _join(UIDS[2])["tournament"]
    assert first["tournament_id"] == second["tournament_id"] == third["tournament_id"]
    assert third["phase"] == "admission"
    assert third["date_ready"] is None
    assert [value["user_id"] for value in third["players"]] == UIDS[:3]
    assert all(value["matches"] == [] for value in third["players"])
    assert tournaments.start_tournament_match(
        UIDS[0], first["tournament_id"], UIDS[1], TYPE1_ADMISSION,
    )["result"] == "WAITING"

    sessions.load_saved_villages()
    restored = tournaments.get_tournament_info(UIDS[0], TYPE1_ADMISSION)["tournament"]
    assert restored["tournament_id"] == first["tournament_id"]
    assert len(restored["players"]) == 3


def test_admission_deadline_fills_only_missing_slots_with_bots():
    _reset_tournament_state()
    for uid in UIDS[:3]:
        room = _join(uid)["tournament"]
    battle = tournaments.get_tournament_info(UIDS[0], TYPE1_BATTLE)["tournament"]
    assert battle["phase"] == "battle"
    assert battle["date_ready"] is not None
    assert len(battle["players"]) == 5
    assert sum(int(value.get("bot", 0)) for value in battle["players"]) == 2
    assert all(len(value["matches"]) == 4 for value in battle["players"])
    assert all(
        not match["finished"]
        for value in battle["players"] if not value.get("bot")
        for match in value["matches"]
    )


def test_five_real_players_fill_room_and_sixth_is_refunded():
    _reset_tournament_state()
    for uid in UIDS[:5]:
        room = _join(uid)["tournament"]
    assert len(room["players"]) == 5
    rejected = _join(UIDS[5])
    assert rejected["result"] == "FULL"
    assert rejected["resources"] == {
        "refund": 1, "tournament_type_id": "1",
    }
    battle = tournaments.get_tournament_info(UIDS[0], TYPE1_BATTLE)["tournament"]
    assert not any(value.get("bot") for value in battle["players"])


def test_leaving_admission_frees_slot_but_cannot_reenter():
    _reset_tournament_state()
    room = _join(UIDS[0])["tournament"]
    _join(UIDS[1])
    assert tournaments.leave_tournament(UIDS[0], TYPE1_ADMISSION)["result"] == "OK"
    assert "activeTournament" not in sessions.session(UIDS[0])["privateState"]
    replacement = _join(UIDS[2])["tournament"]
    assert replacement["tournament_id"] == room["tournament_id"]
    assert [value["user_id"] for value in replacement["players"]] == UIDS[1:3]
    assert _join(UIDS[0])["result"] == "NOK"


def test_battle_lasts_full_day_and_reward_is_credited_once_at_end():
    _reset_tournament_state()
    joined = _join()["tournament"]
    battle = tournaments.get_tournament_info(UID, TYPE1_BATTLE)["tournament"]
    definition = get_game_config()["tournament_type"]["1"]
    prize = definition["prize"][int(joined["reward_id"])]
    before_coins = sessions.session(UID)["maps"][0]["coins"]
    before_cash = sessions.session(UID)["playerInfo"]["cash"]

    _finish_all(battle, 100)
    waiting = tournaments.get_tournament_info(UID, TYPE1_BATTLE)["tournament"]
    assert waiting["date_finished"] is None
    assert waiting["ranking"]["user"]["rank"] == "1"
    assert waiting["reward_credited"] is False
    assert sessions.session(UID)["maps"][0]["coins"] == before_coins

    finished = tournaments.get_tournament_info(UID, TYPE1_FINISHED)["tournament"]
    assert finished["phase"] == "finished"
    assert finished["reward_credited"] is True
    save = sessions.session(UID)
    assert save["maps"][0]["coins"] == before_coins + int(prize.get("g", 0))
    assert save["playerInfo"]["cash"] == before_cash + int(prize.get("c", 0))
    snapshot = json.dumps({
        "coins": save["maps"][0]["coins"],
        "cash": save["playerInfo"]["cash"],
        "store": save["maps"][0]["store"],
    }, sort_keys=True)
    tournaments.get_tournament_info(UID, TYPE1_FINISHED + datetime.timedelta(hours=1))
    save = sessions.session(UID)
    assert snapshot == json.dumps({
        "coins": save["maps"][0]["coins"],
        "cash": save["playerInfo"]["cash"],
        "store": save["maps"][0]["store"],
    }, sort_keys=True)


def test_any_refresh_settles_actual_winner_and_records_history_once():
    _reset_tournament_state()
    _join(UIDS[0])
    _join(UIDS[1])
    battle = tournaments.get_tournament_info(
        UIDS[0], TYPE1_BATTLE,
    )["tournament"]
    _finish_all(battle, 100, UIDS[0])
    _finish_all(battle, 10, UIDS[1])
    definition = get_game_config()["tournament_type"]["1"]
    prize = definition["prize"][int(battle["reward_id"])]
    winner_before = {
        "coins": sessions.session(UIDS[0])["maps"][0]["coins"],
        "cash": sessions.session(UIDS[0])["playerInfo"]["cash"],
    }
    loser_before = {
        "coins": sessions.session(UIDS[1])["maps"][0]["coins"],
        "cash": sessions.session(UIDS[1])["playerInfo"]["cash"],
    }

    # The loser is deliberately first to poll after the deadline. Settlement
    # must still load and credit the winner's independent village save.
    payload = tournaments.get_tournament_info(UIDS[1], TYPE1_FINISHED)
    winner = sessions.session(UIDS[0])
    loser = sessions.session(UIDS[1])
    assert winner["maps"][0]["coins"] == (
        winner_before["coins"] + int(prize.get("g", 0) or 0)
    )
    assert winner["playerInfo"]["cash"] == (
        winner_before["cash"] + int(prize.get("c", 0) or 0)
    )
    assert loser["maps"][0]["coins"] == loser_before["coins"]
    assert loser["playerInfo"]["cash"] == loser_before["cash"]
    history = payload["tournament_winner_history"]
    assert len(history) == 1
    assert history[0]["winner_id"] == UIDS[0]
    assert history[0]["winner_name"] == "Arena Tester 1"
    assert history[0]["tournament_name"]

    # A restart and later refresh must neither duplicate history nor prize.
    snapshot = json.dumps({
        "coins": winner["maps"][0]["coins"],
        "cash": winner["playerInfo"]["cash"],
        "store": winner["maps"][0]["store"],
    }, sort_keys=True)
    sessions.load_saved_villages()
    again = tournaments.get_tournament_info(
        UIDS[1], TYPE1_FINISHED + datetime.timedelta(hours=1),
    )
    assert len(again["tournament_winner_history"]) == 1
    restored = sessions.session(UIDS[0])
    assert snapshot == json.dumps({
        "coins": restored["maps"][0]["coins"],
        "cash": restored["playerInfo"]["cash"],
        "store": restored["maps"][0]["store"],
    }, sort_keys=True)


def test_leave_and_clean_validate_battle_lifecycle():
    _reset_tournament_state()
    room = _join()["tournament"]
    assert tournaments.clean_tournament(UID, TYPE1_ADMISSION)["result"] == "NOK"
    tournaments.get_tournament_info(UID, TYPE1_BATTLE)
    assert tournaments.leave_tournament(UID, TYPE1_BATTLE)["result"] == "OK"
    state = tournaments._load_state()
    player = _player(state["rooms"][room["tournament_id"]])
    assert player["abandonned"] == 1
    assert all(match["finished"] for match in player["matches"])
    assert _join(UID, "1", TYPE1_ADMISSION)["result"] in ("NOK", "FULL")

    _reset_tournament_state()
    _join()
    assert tournaments.clean_tournament(UID, TYPE1_BATTLE)["result"] == "NOK"
    tournaments.get_tournament_info(UID, TYPE1_FINISHED)
    assert tournaments.clean_tournament(UID, TYPE1_FINISHED)["result"] == "OK"
    assert "activeTournament" not in sessions.session(UID)["privateState"]


def test_http_protocol_still_returns_complete_schedule():
    _reset_tournament_state()
    data = _post("get_tournament_info", {"user_id": UID})
    assert data["tournament_friends"] == {}
    assert set(data["tournament_daily"]) == set(str(value) for value in range(1, 8))
    assert set(data["tournament_weekly"]) == {"8"}
    assert data["tournament_winner_history"] == []


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
    assert sessions.session(UID)["privateState"]["teams"]["tournament"] == TEAM


def test_bundled_client_has_schedule_reward_and_shared_room_patches():
    assert patched(DEFAULT_SWF.read_bytes())
    assert shared_patched(SHARED_SWF.read_bytes())
    assert history_patched(HISTORY_SWF.read_bytes())


TESTS = [
    test_non_overlapping_two_day_rotation,
    test_all_eight_types_take_one_rotation_slot,
    test_config_has_five_slots_four_bots_and_renamed_gold_event,
    test_real_players_share_one_admission_room_and_restart_keeps_it,
    test_admission_deadline_fills_only_missing_slots_with_bots,
    test_five_real_players_fill_room_and_sixth_is_refunded,
    test_leaving_admission_frees_slot_but_cannot_reenter,
    test_battle_lasts_full_day_and_reward_is_credited_once_at_end,
    test_any_refresh_settles_actual_winner_and_records_history_once,
    test_leave_and_clean_validate_battle_lifecycle,
    test_http_protocol_still_returns_complete_schedule,
    test_fee_and_refund_commands_cancel_out,
    test_selected_tournament_team_survives_reload,
    test_bundled_client_has_schedule_reward_and_shared_room_patches,
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
