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
    """A minimal valid save built from a shipped throwaway save."""
    src = os.path.join("saves", "d609236a-80d6-4bf5-9602-ab6d699fb6e0.save.json")
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


TESTS = [
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
