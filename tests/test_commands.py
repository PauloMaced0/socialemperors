"""Command-handler tests: gift/storage guards, resurrect payment, Monday and
comeback bonuses, treasure collection, buy-with-cash, neighbour aliasing.

    /path/to/.venv/bin/python tests/test_commands.py

Plain asserts, isolated temp saves dir; never touches ./saves.
"""
import os
import sys
import json
import shutil
import tempfile
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import command
import engine
from constants import Constant
from get_game_config import get_game_config

UID = "test-cmd-0001"

# item 535 (Ranger): cost 250 g, cost_unit_cash 3, potion 2
RANGER = 535


def _template_save():
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = UID
    save["playerInfo"]["cash"] = 0
    m = save["maps"][0]
    m["coins"] = 0
    m["wood"] = m["stone"] = m["food"] = 0
    save["privateState"]["gifts"] = []
    save["privateState"]["potion"] = 0
    return save


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="se_cmd_")
    sessions.SAVES_DIR = tmp
    json.dump(_template_save(), open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    return tmp


def _do(cmd, args):
    command.do_command(UID, cmd, args)


def _now(t):
    command.timestamp_now = lambda: t
    engine.timestamp_now = lambda: t


def _local(day, hour):
    "Epoch seconds for 1970-01-<day> <hour>:00 LOCAL time. Jan 12 1970 = Monday."
    return int(datetime.datetime(1970, 1, day, hour, 0).timestamp())


def _items(save):
    return save["maps"][0]["items"]


# --- gift / storage placement guards --------------------------------------

def test_place_gift_without_stock_rejected(tmp):
    save = sessions.session(UID)
    n_items = len(_items(save))
    _do(Constant.CMD_PLACE_GIFT, [RANGER, 10, 10, 0, 0])
    assert len(_items(save)) == n_items, "item placed without owning the gift (dupe exploit)"
    _do(Constant.CMD_PLACE_STORED_ITEM, [RANGER, 10, 10, 0, 0])
    assert len(_items(save)) == n_items, "stored item placed without stock"


def test_place_gift_and_stored_item_consume_storage(tmp):
    save = sessions.session(UID)
    save["privateState"]["gifts"] = [0] * RANGER + [2]
    n_items = len(_items(save))
    _do(Constant.CMD_PLACE_GIFT, [RANGER, 10, 10, 0, 0])
    _do(Constant.CMD_PLACE_STORED_ITEM, [RANGER, 11, 10, 0, 0])
    assert len(_items(save)) == n_items + 2, "placements not persisted"
    assert _items(save)[-1][0] == RANGER, "wrong item id placed"
    assert save["privateState"]["gifts"] == [], "gift count not consumed / zeros not trimmed"


def test_sell_gift_and_stored_item(tmp):
    save = sessions.session(UID)
    _do(Constant.CMD_SELL_GIFT, [RANGER, 0])
    assert save["maps"][0]["coins"] == 0, "sold a gift that was never owned"
    save["privateState"]["gifts"] = [0] * RANGER + [1]
    _do(Constant.CMD_SELL_STORED, [RANGER, 0])
    assert save["privateState"]["gifts"] == [], "sale did not consume storage"
    # 5% refund of 250 gold cost = int(-12.5) -> 12
    assert save["maps"][0]["coins"] == 12, f"5%% refund wrong: {save['maps'][0]['coins']}"


# --- resurrect payment -----------------------------------------------------

def test_resurrect_with_potions(tmp):
    save = sessions.session(UID)
    save["privateState"]["potion"] = 2  # Ranger needs 2
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0, "1"])
    assert _items(save)[-1][0] == RANGER, "unit not placed"
    assert save["privateState"]["potion"] == 0, "potions not consumed"


def test_resurrect_potion_shortage_rejected(tmp):
    save = sessions.session(UID)
    save["privateState"]["potion"] = 1  # needs 2
    n_items = len(_items(save))
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0, "1"])
    assert len(_items(save)) == n_items, "free resurrect leaked (potion shortage)"
    assert save["privateState"]["potion"] == 1, "potions deducted on rejected resurrect"


def test_resurrect_graveyard_resource_price(tmp):
    save = sessions.session(UID)
    m = save["maps"][0]
    m["food"], m["coins"] = 250, 125  # Ranger: food=cost, gold=cost/2
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0, "0"])
    assert _items(save)[-1][0] == RANGER, "unit not placed"
    assert m["food"] == 0 and m["coins"] == 0, f"resource price not charged: {m['food']}f {m['coins']}g"


def test_resurrect_hero_gold_price(tmp):
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 1500  # cost_unit_cash 3 * RESURRECT_MULTIPLIER 500
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0])
    assert _items(save)[-1][0] == RANGER, "unit not placed"
    assert m["coins"] == 0, f"gold price not charged: {m['coins']}"
    n_items = len(_items(save))
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0])  # broke now
    assert len(_items(save)) == n_items, "free resurrect leaked (no gold)"


# --- Monday + comeback bonuses ---------------------------------------------

def test_monday_bonus_gold_once_per_monday(tmp):
    _now(_local(12, 10))  # Monday
    save = sessions.session(UID)
    _do(Constant.CMD_COLLECT_MONDAY_BONUS, ["g", 999999])
    assert save["maps"][0]["coins"] == 2500, "config gold amount not used (client value trusted?)"
    _do(Constant.CMD_COLLECT_MONDAY_BONUS, ["g", 2500])
    assert save["maps"][0]["coins"] == 2500, "second same-day Monday claim leaked"


def test_monday_bonus_rejected_off_monday(tmp):
    _now(_local(13, 10))  # Tuesday
    save = sessions.session(UID)
    _do(Constant.CMD_COLLECT_MONDAY_BONUS, ["g", 2500])
    assert save["maps"][0]["coins"] == 0, "Monday bonus granted on a Tuesday"


def test_monday_bonus_unit_whitelisted(tmp):
    _now(_local(12, 10))
    save = sessions.session(UID)
    units = [int(u) for u in get_game_config()["globals"]["MONDAY_BONUS_UNITS"]]
    _do(Constant.CMD_COLLECT_MONDAY_BONUS, ["u", units[1]])
    gifts = save["privateState"]["gifts"]
    assert len(gifts) > units[1] and gifts[units[1]] == 1, "whitelisted unit not stored"


def test_comeback_bonus_day_gate(tmp):
    _now(_local(14, 10))
    save = sessions.session(UID)
    _do(Constant.CMD_COLLECT_COMEBACK_BONUS, ["c", 999, 0])
    assert save["playerInfo"]["cash"] == 5, "config cash amount not used"
    _do(Constant.CMD_COLLECT_COMEBACK_BONUS, ["c", 5, 0])
    assert save["playerInfo"]["cash"] == 5, "repeat comeback day leaked"
    _do(Constant.CMD_COLLECT_COMEBACK_BONUS, ["c", 5, 1])
    assert save["playerInfo"]["cash"] == 10, "next comeback day blocked"


# --- treasure + buy with cash ----------------------------------------------

def test_collect_treasure_clamped_and_persisted(tmp):
    save = sessions.session(UID)
    m = save["maps"][0]
    xp_before = int(m["xp"])
    _do(Constant.CMD_COLLECT_TREASURE, [999999, 999999, 3, 0, 0, 0])
    assert m["coins"] == 1500, f"gold not clamped: {m['coins']}"
    assert int(m["xp"]) == xp_before + 200, f"xp not clamped: {m['xp']}"
    assert m["idCurrentTreasure"] == 3, "next quest id not persisted"


def test_collect_treasure_stamps_kill_time(tmp):
    # Killing the enemy camp must persist timestampLastTreasure; the client
    # gates the camp respawn on it, so without this a reload respawns the
    # enemies the player just cleared.
    _now(_local(14, 12))
    save = sessions.session(UID)
    save["maps"][0]["timestampLastTreasure"] = 0
    _do(Constant.CMD_COLLECT_TREASURE, [100, 20, 1, 0, 0, 0])
    assert save["maps"][0]["timestampLastTreasure"] == _local(14, 12), \
        "camp kill time not persisted -> enemies respawn on reload"


def test_buy_unit_with_cash(tmp):
    save = sessions.session(UID)
    n_items = len(_items(save))
    _do(Constant.CMD_BUY_UNIT_WITH_CASH, [RANGER, 8, 8, 0, 0])
    assert len(_items(save)) == n_items, "cash purchase leaked with 0 cash"
    save["playerInfo"]["cash"] = 3
    _do(Constant.CMD_BUY_UNIT_WITH_CASH, [RANGER, 8, 8, 0, 0])
    assert _items(save)[-1][0] == RANGER, "bought unit not placed"
    assert save["playerInfo"]["cash"] == 0, "cash price not charged"


# --- neighbour aliasing ------------------------------------------------------

def test_neighbors_does_not_pollute_playerinfo(tmp):
    sessions.neighbors(UID)
    static = sessions.neighbor_session("1111")  # AcidCaos static village
    for k in ("coins", "xp", "level", "stone", "wood", "food"):
        assert k not in static["playerInfo"], f"neighbors() leaked '{k}' into stored playerInfo"


def test_loading_scrubs_leaked_playerinfo_fields(tmp):
    polluted = _template_save()
    polluted["playerInfo"]["coins"] = 123
    polluted["playerInfo"]["wood"] = 9
    json.dump(polluted, open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    save = sessions.session(UID)
    assert "coins" not in save["playerInfo"], "leaked coins not scrubbed on load"
    assert "wood" not in save["playerInfo"], "leaked wood not scrubbed on load"


TESTS = [
    test_place_gift_without_stock_rejected,
    test_place_gift_and_stored_item_consume_storage,
    test_sell_gift_and_stored_item,
    test_resurrect_with_potions,
    test_resurrect_potion_shortage_rejected,
    test_resurrect_graveyard_resource_price,
    test_resurrect_hero_gold_price,
    test_monday_bonus_gold_once_per_monday,
    test_monday_bonus_rejected_off_monday,
    test_monday_bonus_unit_whitelisted,
    test_comeback_bonus_day_gate,
    test_collect_treasure_clamped_and_persisted,
    test_collect_treasure_stamps_kill_time,
    test_buy_unit_with_cash,
    test_neighbors_does_not_pollute_playerinfo,
    test_loading_scrubs_leaked_playerinfo_fields,
]


def main():
    passed = failed = 0
    for t in TESTS:
        tmp = _fresh_env()
        _now(_local(14, 10))
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
