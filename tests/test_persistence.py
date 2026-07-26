"""Persistence tests. Run from repo root:

    /path/to/.venv/bin/python tests/test_persistence.py

No pytest dependency: plain asserts, prints PASS/FAIL, non-zero exit on failure.
Uses an isolated temporary saves dir; never touches the real ./saves.
"""
import os
import sys
import json
import copy
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import command
import engine
from constants import Constant

UID = "test-uid-0001"
FARM_ID = 10          # "Farm Land", collect 15 food
FARM_XY = (30, 30)
OLD_TS = 1000000000   # year 2001, safely in the past


def _template_save():
    """A minimal valid save built from the shipped initial village."""
    src = os.path.join("villages", "initial.json")
    save = json.load(open(src))
    save["playerInfo"]["pid"] = UID
    m = save["maps"][0]
    # place a farm with a stale collect timestamp
    m["items"] = [it for it in m["items"] if not (it[0] == FARM_ID and it[1] == FARM_XY[0] and it[2] == FARM_XY[1])]
    m["items"].append([FARM_ID, FARM_XY[0], FARM_XY[1], 0, OLD_TS, 0])
    return save


def _fresh_env():
    """Point sessions/command at a clean temp saves dir with one test save."""
    tmp = tempfile.mkdtemp(prefix="se_test_")
    sessions.SAVES_DIR = tmp
    if hasattr(command, "SAVES_DIR"):
        command.SAVES_DIR = tmp
    json.dump(_template_save(), open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    return tmp


def _find_farm(save):
    for it in save["maps"][0]["items"]:
        if it[0] == FARM_ID and it[1] == FARM_XY[0] and it[2] == FARM_XY[1]:
            return it
    return None


def _batch(commands):
    return {
        "ts": 1, "first_number": 1, "accessToken": "x", "tries": 0,
        "publishActions": [], "commands": commands,
    }


# --- Tests ---------------------------------------------------------------

def test_collect_advances_item_timestamp(tmp):
    """CMD_COLLECT must stamp the collected item so it enters cooldown."""
    command.command(UID, _batch([
        {"cmd": Constant.CMD_COLLECT, "args": [FARM_XY[0], FARM_XY[1], 0, FARM_ID, 0, 1, 0]},
    ]))
    farm = _find_farm(sessions.session(UID))
    assert farm is not None, "farm vanished"
    now = engine.timestamp_now()
    assert farm[4] != OLD_TS, f"item timestamp not advanced (still {OLD_TS})"
    assert now - 10 <= farm[4] <= now, f"item timestamp not ~now: {farm[4]} vs {now}"


def test_batch_persists_despite_failing_command(tmp):
    """A throwing command must not discard successful earlier commands."""
    # buy a farm (valid), then a move with no args (raises IndexError), then buy another
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY, "args": [FARM_ID, 40, 40, 0, 0, 0, 1, 0]},
        {"cmd": Constant.CMD_MOVE, "args": []},
        {"cmd": Constant.CMD_BUY, "args": [FARM_ID, 41, 41, 0, 0, 0, 1, 0]},
    ]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    items = disk["maps"][0]["items"]
    assert any(it[0] == FARM_ID and it[1] == 40 and it[2] == 40 for it in items), \
        "first buy (before the failing command) was not persisted to disk"
    assert any(it[0] == FARM_ID and it[1] == 41 and it[2] == 41 for it in items), \
        "buy after the failing command was not persisted to disk"


def test_atomic_save_leaves_no_tmp_file(tmp):
    """save_session writes valid JSON and leaves no leftover temp file."""
    sessions.save_session(UID)
    leftovers = [f for f in os.listdir(tmp) if f.endswith(".tmp") or f.endswith(".part")]
    assert not leftovers, f"leftover temp files: {leftovers}"
    json.load(open(os.path.join(tmp, f"{UID}.save.json")))  # valid JSON


def test_unhandled_command_is_logged(tmp):
    """Unknown commands must be captured to a log for later implementation."""
    command.command(UID, _batch([
        {"cmd": "totally_fake_cmd", "args": [1, 2, 3]},
    ]))
    log = os.path.join(tmp, "unhandled_commands.log")
    assert os.path.exists(log), "unhandled_commands.log not created"
    content = open(log).read()
    assert "totally_fake_cmd" in content, "cmd name not logged"
    assert "[1, 2, 3]" in content or "1, 2, 3" in content, "args not logged"


def test_activate_persists_working_state(tmp):
    """CMD_ACTIVATE must persist attrs.cp (the chosen time option) AND stamp
    collected_at, so the client sees the producer still working after a reload.
    The client reads the working state from item[7]['cp']; without it the mine
    reverts to idle ("assign a worker again")."""
    PROD_ID, PX, PY = 5, 60, 60  # Mill I
    m = sessions.session(UID)["maps"][0]
    m["items"].append([PROD_ID, PX, PY, 0, OLD_TS, 0, [500, 500]])
    # activate(x, y, town_id, item_id, time_option); option 3 = 4h
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ACTIVATE, "args": [PX, PY, 0, PROD_ID, 3]},
    ]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    prod = next((it for it in disk["maps"][0]["items"]
                 if it[0] == PROD_ID and it[1] == PX and it[2] == PY), None)
    assert prod is not None, "producer vanished"
    assert len(prod) >= 8 and isinstance(prod[7], dict), "producer has no attrs object"
    assert prod[7].get("cp") == 3, f"attrs.cp not persisted: {prod[7]}"
    now = engine.timestamp_now()
    assert now - 10 <= prod[4] <= now, f"production start not stamped: {prod[4]}"


def test_activate_zero_stops_without_restamping(tmp):
    """activate option 0 (reset/stop) clears cp and leaves the start time alone."""
    PROD_ID, PX, PY = 5, 61, 61
    m = sessions.session(UID)["maps"][0]
    m["items"].append([PROD_ID, PX, PY, 0, OLD_TS, 0, [], {"cp": 3}])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ACTIVATE, "args": [PX, PY, 0, PROD_ID, 0]},
    ]))
    prod = next(it for it in sessions.session(UID)["maps"][0]["items"]
                if it[0] == PROD_ID and it[1] == PX and it[2] == PY)
    assert prod[7].get("cp") == 0, "cp not cleared on stop"
    assert prod[4] == OLD_TS, "start time should not be restamped on stop"


def test_end_attack_win_marks_conquered_and_rewards(tmp):
    """Winning a PvP war must record the conquered island position in
    universAttackWin (so it shows complete) and grant the battle rewards."""
    m = sessions.session(UID)["maps"][0]
    m["universAttackWin"] = []
    m["coins"] = 1000
    m["xp"] = 500
    cash_before = sessions.session(UID)["playerInfo"]["cash"]
    payload = json.dumps({
        "attacker": {"map": 0}, "victim": {"posicion": 3, "user_id": "1111"},
        "resources": {"g": 250, "x": 40}, "win": 1,
    })
    command.command(UID, _batch([{"cmd": Constant.CMD_END_ATTACK, "args": [payload]}]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    dm = disk["maps"][0]
    assert 3 in dm["universAttackWin"], f"conquered position not recorded: {dm['universAttackWin']}"
    assert dm["coins"] == 1250, f"gold reward not applied: {dm['coins']}"
    assert dm["xp"] == 540, f"xp reward not applied: {dm['xp']}"
    assert disk["playerInfo"]["cash"] == cash_before + 1, \
        "first win against the player did not grant one cash"
    # winning the same island again must not duplicate it
    command.command(UID, _batch([{"cmd": Constant.CMD_END_ATTACK, "args": [payload]}]))
    dm2 = sessions.session(UID)["maps"][0]
    assert dm2["universAttackWin"].count(3) == 1, "conquered position duplicated"
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_before + 1, \
        "replaying the same opponent granted the one-cash reward again"


def test_end_attack_loss_does_not_mark_conquered(tmp):
    """A lost war must not mark the island conquered."""
    m = sessions.session(UID)["maps"][0]
    m["universAttackWin"] = []
    payload = json.dumps({
        "attacker": {"map": 0}, "victim": {"posicion": 2},
        "resources": {"g": 0, "x": 0}, "win": 0,
    })
    command.command(UID, _batch([{"cmd": Constant.CMD_END_ATTACK, "args": [payload]}]))
    assert 2 not in sessions.session(UID)["maps"][0]["universAttackWin"]


def test_end_quest_win_saves_star_rank(tmp):
    """Completing a quest island must record its star rank (difficulty) in
    privateState.questsRank, keyed by the quest id string, keeping the best."""
    ps = sessions.session(UID)["privateState"]
    ps["questsRank"] = {}
    def end(qid, diff, win=1):
        payload = json.dumps({
            "map": 0, "resources": {"g": 0, "x": 0}, "units": [], "win": win,
            "duration": 60, "voluntary_end": 0, "quest_id": qid, "difficulty": diff,
        })
        command.command(UID, _batch([{"cmd": Constant.CMD_END_QUEST, "args": [payload]}]))
    end("100000006", 2)
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    assert disk["privateState"]["questsRank"].get("100000006") == 2, \
        f"star rank not saved: {disk['privateState']['questsRank']}"
    end("100000006", 1)  # worse run must not lower the rank
    assert sessions.session(UID)["privateState"]["questsRank"]["100000006"] == 2
    end("100000006", 3)  # better run raises it
    assert sessions.session(UID)["privateState"]["questsRank"]["100000006"] == 3
    end("100000007", 2, win=0)  # a loss records nothing
    assert "100000007" not in sessions.session(UID)["privateState"]["questsRank"]


def test_store_item_frombug_moves_item_to_storage(tmp):
    """store_item_frombug relocates an owned item to map.store.

    Keeping owned storage separate from gifts is what prevents re-placement
    from awarding the construction XP again after a browser reload.
    """
    ITEM_ID, IX, IY = 224, 53, 59  # Harbor
    m = sessions.session(UID)["maps"][0]
    m["items"].append([ITEM_ID, IX, IY, 0, 0, 0])
    m["store"] = {}
    command.command(UID, _batch([
        {"cmd": Constant.CMD_STORE_ITEM_FROMBUG, "args": [IX, IY, 0, ITEM_ID]},
    ]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    still = [it for it in disk["maps"][0]["items"]
             if it[0] == ITEM_ID and it[1] == IX and it[2] == IY]
    assert not still, "colliding item was not removed from the map"
    assert disk["maps"][0]["store"][str(ITEM_ID)] == 1, \
        "item not added to owned storage"


def test_darts_flow_persists(tmp):
    """Daily darts: reset clears the board + seeds it, each throw records the
    balloon, and prizes (store_add_items) land in storage."""
    ps = sessions.session(UID)["privateState"]
    ps["gifts"] = []
    ps["dartsBalloonsShot"] = [9]
    # reset: clears board, stamps timestamps, stores seed
    command.command(UID, _batch([{"cmd": Constant.CMD_DARTS_RESET, "args": [4242]}]))
    ps = sessions.session(UID)["privateState"]
    now = engine.timestamp_now()
    assert ps["dartsBalloonsShot"] == [], "board not cleared on reset"
    assert ps["dartsRandomSeed"] == 4242, "seed not stored"
    assert now - 10 <= ps["timeStampDartsReset"] <= now, "reset timestamp wrong"
    # throw at balloon 3 (free throw)
    command.command(UID, _batch([{"cmd": Constant.CMD_DARTS_SHOOT_BALLOON, "args": [3, 0, 0]}]))
    assert 3 in sessions.session(UID)["privateState"]["dartsBalloonsShot"], "throw not recorded"
    # prize goes to storage via store_add_items([ids])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_STORE_ADD_ITEMS, "args": [json.dumps([176, 176, 180])]},
    ]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    gifts = disk["privateState"]["gifts"]
    assert gifts[176] == 2 and gifts[180] == 1, f"prizes not stored: 176={gifts[176]}, 180={gifts[180]}"


def test_darts_new_free_stamps_claim(tmp):
    """Claiming the daily free stamps timeStampDartsNewFree so it can't be reclaimed."""
    command.command(UID, _batch([{"cmd": Constant.CMD_DARTS_NEW_FREE, "args": []}]))
    now = engine.timestamp_now()
    ts = sessions.session(UID)["privateState"]["timeStampDartsNewFree"]
    assert now - 10 <= ts <= now, f"free-claim timestamp not set: {ts}"


def test_unit_collection_completed_grants_cash_once(tmp):
    """Completing a unit collection marks it done and grants +1 cash, once."""
    ps = sessions.session(UID)["privateState"]
    ps["unitCollectionsCompleted"] = []
    sessions.session(UID)["playerInfo"]["cash"] = 0
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [5]}]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    assert 5 in disk["privateState"]["unitCollectionsCompleted"], "collection not marked done"
    assert disk["playerInfo"]["cash"] == 1, f"cash reward not granted: {disk['playerInfo']['cash']}"
    # re-sending the same collection must not grant cash again
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [5]}]))
    assert sessions.session(UID)["playerInfo"]["cash"] == 1, "cash double-granted"
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [6]}]))
    assert sessions.session(UID)["playerInfo"]["cash"] == 2, "second collection cash missing"


def test_apply_rewards_ranking_grants_cash_and_items(tmp):
    """Island ranking reward adds cash and drops item rewards into storage."""
    sessions.session(UID)["playerInfo"]["cash"] = 10
    sessions.session(UID)["privateState"]["gifts"] = []
    command.command(UID, _batch([
        {"cmd": Constant.CMD_APPLY_REWARDS_RANKING, "args": [3, 50, json.dumps([176, 180])]},
    ]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    assert disk["playerInfo"]["cash"] == 60, f"ranking cash not added: {disk['playerInfo']['cash']}"
    g = disk["privateState"]["gifts"]
    assert g[176] == 1 and g[180] == 1, "ranking items not stored"


TESTS = [
    test_unit_collection_completed_grants_cash_once,
    test_apply_rewards_ranking_grants_cash_and_items,
    test_darts_flow_persists,
    test_darts_new_free_stamps_claim,
    test_store_item_frombug_moves_item_to_storage,
    test_end_quest_win_saves_star_rank,
    test_end_attack_win_marks_conquered_and_rewards,
    test_end_attack_loss_does_not_mark_conquered,
    test_collect_advances_item_timestamp,
    test_batch_persists_despite_failing_command,
    test_atomic_save_leaves_no_tmp_file,
    test_unhandled_command_is_logged,
    test_activate_persists_working_state,
    test_activate_zero_stops_without_restamping,
]


def main():
    passed = failed = 0
    for t in TESTS:
        tmp = _fresh_env()
        try:
            t(tmp)
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
