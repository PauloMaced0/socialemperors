"""Economy tests: level-up rewards (GD-04) and daily bonus (GD-05).

    /path/to/.venv/bin/python tests/test_economy.py

Plain asserts, isolated temp saves dir; never touches ./saves.
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import command
import engine
from constants import Constant
from get_game_config import get_game_config

UID = "test-econ-0001"


def _template_save():
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = UID
    save["playerInfo"]["cash"] = 0
    m = save["maps"][0]
    m["level"] = 1
    m["xp"] = 0
    m["coins"] = 0
    m["wood"] = m["stone"] = m["food"] = 0
    save["privateState"]["gifts"] = []
    save["privateState"]["bonusNextId"] = 0
    save["privateState"]["timestampLastBonus"] = 0
    return save


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="se_econ_")
    sessions.SAVES_DIR = tmp
    json.dump(_template_save(), open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    return tmp


def _do(cmd, args):
    command.do_command(UID, cmd, args)


def _now(t):
    command.timestamp_now = lambda: t
    engine.timestamp_now = lambda: t


# --- GD-04: level-up rewards ---------------------------------------------

def test_levelup_grants_cash_at_level_5(tmp):
    _do(Constant.CMD_RT_LEVEL_UP, [5])
    save = sessions.session(UID)
    assert save["maps"][0]["level"] == 5, "level not set"
    # levels[1..4] rewards: w50, f50, w400, c1  -> cash must be exactly 1
    assert save["playerInfo"]["cash"] == 1, f"level-up cash reward missing: {save['playerInfo']['cash']}"
    assert save["maps"][0]["wood"] == 450, f"wood rewards wrong: {save['maps'][0]['wood']}"
    assert save["maps"][0]["food"] == 50, f"food reward wrong: {save['maps'][0]['food']}"


def test_levelup_is_idempotent(tmp):
    _do(Constant.CMD_RT_LEVEL_UP, [5])
    cash_after_first = sessions.session(UID)["playerInfo"]["cash"]
    _do(Constant.CMD_RT_LEVEL_UP, [5])  # same level again
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_after_first, \
        "re-sending same level-up double-granted the reward"


def test_levelup_single_step_grants_only_that_level(tmp):
    # level 4 reward = levels[3] = wood 400 (no cash); only level 5 = levels[4]
    # = 1 cash. Stepping 1->4 then 4->5 must grant the cash exactly once.
    _do(Constant.CMD_RT_LEVEL_UP, [4])
    assert sessions.session(UID)["playerInfo"]["cash"] == 0, "level 4 should grant no cash (wood 400)"
    _do(Constant.CMD_RT_LEVEL_UP, [5])
    assert sessions.session(UID)["playerInfo"]["cash"] == 1, "level 5 should grant exactly 1 cash"


# --- GD-05: daily bonus ---------------------------------------------------

def test_daily_bonus_uses_config_not_client(tmp):
    _now(1000000)
    # client tries to inject 999 cash / 999 gold / hero 5
    _do(Constant.CMD_WIN_BONUS, [999, 0, 5, 0, 999])
    save = sessions.session(UID)
    # config[0] = 250 gold; client values must be ignored
    assert save["playerInfo"]["cash"] == 0, f"client cash injected: {save['playerInfo']['cash']}"
    assert save["maps"][0]["coins"] == 250, f"expected 250 gold from config, got {save['maps'][0]['coins']}"
    assert save["privateState"]["bonusNextId"] == 1, "streak not advanced"


def test_daily_bonus_cooldown_blocks_second_claim(tmp):
    _now(1000000)
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])          # claim #1 -> 250 gold
    coins1 = sessions.session(UID)["maps"][0]["coins"]
    _now(1000000 + 3600)                                   # +1h, still same day
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 1, 999])         # attempt #2
    save = sessions.session(UID)
    assert save["maps"][0]["coins"] == coins1, "second same-day claim was NOT blocked"
    assert save["playerInfo"]["cash"] == 0, "second claim leaked cash"
    assert save["privateState"]["bonusNextId"] == 1, "blocked claim advanced streak"


def test_daily_bonus_next_day_gives_next_reward(tmp):
    _now(1000000)
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])           # #1 -> 250 gold
    _now(1000000 + 86400)                                  # +24h
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])           # #2 -> config[1] = hero
    heroes = get_game_config()["globals"]["DAILY_BONUS_CONFIG_HEROES"]
    hero_id = int(heroes[1 % len(heroes)])
    gifts = sessions.session(UID)["privateState"]["gifts"]
    assert len(gifts) > hero_id and gifts[hero_id] >= 1, "day-2 hero bonus not granted"
    assert sessions.session(UID)["privateState"]["bonusNextId"] == 2, "streak not advanced to 2"


# --- Darts: one free throw per day, extras billed ------------------------

def test_darts_free_then_billed_then_broke(tmp):
    _now(1000000)
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 25
    save["privateState"]["dartsBalloonsShot"] = []
    save["privateState"]["timeStampLastDart"] = 0
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [1, 0, 0])   # free
    assert save["privateState"]["dartsBalloonsShot"] == [1], "free throw not recorded"
    assert save["playerInfo"]["cash"] == 25, "free throw wrongly billed"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 0, 0])   # extra -> billed 20
    assert save["privateState"]["dartsBalloonsShot"] == [1, 2], "paid throw not recorded"
    assert save["playerInfo"]["cash"] == 5, f"extra throw not billed: {save['playerInfo']['cash']}"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [3, 0, 0])   # broke -> rejected
    assert save["privateState"]["dartsBalloonsShot"] == [1, 2], "broke throw not rejected"
    assert save["playerInfo"]["cash"] == 5, "broke throw changed cash"


def test_darts_free_returns_after_24h(tmp):
    _now(1000000)
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 0
    save["privateState"]["dartsBalloonsShot"] = []
    save["privateState"]["timeStampLastDart"] = 0
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [1, 0, 0])   # free today
    _now(1000000 + 3600)
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 0, 0])   # +1h, no cash -> rejected
    assert save["privateState"]["dartsBalloonsShot"] == [1], "same-day free repeat leaked"
    _now(1000000 + 86400)
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 0, 0])   # +24h -> free again
    assert save["privateState"]["dartsBalloonsShot"] == [1, 2], "next-day free throw blocked"
    assert save["playerInfo"]["cash"] == 0, "next-day free throw billed"


TESTS = [
    test_levelup_grants_cash_at_level_5,
    test_levelup_is_idempotent,
    test_levelup_single_step_grants_only_that_level,
    test_daily_bonus_uses_config_not_client,
    test_daily_bonus_cooldown_blocks_second_claim,
    test_daily_bonus_next_day_gives_next_reward,
    test_darts_free_then_billed_then_broke,
    test_darts_free_returns_after_24h,
]


def main():
    passed = failed = 0
    for t in TESTS:
        tmp = _fresh_env()
        # reset time function each test
        _now(1000000)
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
