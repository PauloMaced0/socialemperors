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
    m["store"] = {}
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
    save["privateState"]["gifts"] = [0] * RANGER + [1]
    save["maps"][0]["store"] = {str(RANGER): 1}
    n_items = len(_items(save))
    _do(Constant.CMD_PLACE_GIFT, [RANGER, 10, 10, 0, 0])
    _do(Constant.CMD_PLACE_STORED_ITEM, [RANGER, 11, 10, 0, 0])
    assert len(_items(save)) == n_items + 2, "placements not persisted"
    assert _items(save)[-1][0] == RANGER, "wrong item id placed"
    assert save["privateState"]["gifts"] == [], "gift count not consumed"
    assert save["maps"][0]["store"] == {}, "owned storage count not consumed"


def test_sell_gift_and_stored_item(tmp):
    save = sessions.session(UID)
    _do(Constant.CMD_SELL_GIFT, [RANGER, 0])
    assert save["maps"][0]["coins"] == 0, "sold a gift that was never owned"
    save["privateState"]["gifts"] = [0] * RANGER + [1]
    save["maps"][0]["store"] = {str(RANGER): 1}
    _do(Constant.CMD_SELL_GIFT, [RANGER, 0])
    _do(Constant.CMD_SELL_STORED, [RANGER, 0])
    assert save["privateState"]["gifts"] == [], "gift sale did not consume gift"
    assert save["maps"][0]["store"] == {}, "stored sale did not consume owned item"
    # Each 5% refund of the 250 gold cost is int(-12.5) -> 12.
    assert save["maps"][0]["coins"] == 24, f"5%% refunds wrong: {save['maps'][0]['coins']}"


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

def test_allies_building_opens_once_and_never_re_asks(tmp):
    # Allies Building (Summit, id 234): buying its allies with cash spawns the
    # cavaliers, and the building must then stay OPENED (si=None) so a reload
    # doesn't request payment again.
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 100
    summit = Constant.ID_BUILDING_SUMMIT
    save["maps"][0]["items"].append([summit, 60, 60, 0, 0, 0, [], {}])
    social = command._social_item(summit)
    required = command._social_worker_count(social)

    for _ in range(required):
        _do(Constant.CMD_BUY_SI_HELP, [60, 60, 0, summit])
    _do(Constant.CMD_FINISH_SI, [60, 60, 0, summit])

    item = command._find_map_item(save["maps"][0], summit, 60, 60)
    assert command._item_attrs(item).get("si", 0) is None, \
        "Allies Building did not stay opened -> reload would re-ask for payment"

    # A further staffing purchase after opening is rejected (no double charge).
    cash_after_open = save["playerInfo"]["cash"]
    ok = command.do_command(UID, Constant.CMD_BUY_SI_HELP, [60, 60, 0, summit])
    assert ok is False, "opened Allies Building accepted another paid worker"
    assert save["playerInfo"]["cash"] == cash_after_open, "re-charged after opening"


def test_festival_decoration_harvest_pays_gold_but_not_quest_treasure(tmp):
    # Festival/monument decorations (subcat 811) are harvested once and
    # consumed via SELL_REASON_TREASURE; pay a fixed gold reward instead of 0.
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 0
    festival = 487  # Chinese Festival
    m["items"].append([festival, 60, 60, 0, 0, 0])
    _do(Constant.CMD_SELL, [60, 60, festival, 0, 0, Constant.SELL_REASON_TREASURE])
    # 1000 treasure gold plus the ordinary 5% resale credit on the 300g cost.
    assert m["coins"] >= Constant.TREASURE_DECORATION_GOLD, \
        "festival harvest did not pay its treasure gold"
    assert not any(it[1:3] == [60, 60] for it in m["items"]), \
        "harvested festival not consumed"

    # A quest camp treasure shares the subcat/reason but must NOT be paid here
    # (its reward comes from CMD_COLLECT_TREASURE) - no double reward.
    m["coins"] = 0
    chest = Constant.ID_BUILDING_TREASURE_CHEST
    m["items"].append([chest, 61, 61, 0, 0, 0])
    _do(Constant.CMD_SELL, [61, 61, chest, 0, 0, Constant.SELL_REASON_TREASURE])
    assert m["coins"] == 0, "quest treasure chest wrongly paid festival gold"

    # An enemy-guarded festival (Viking Festival) must NOT pay on the ungated
    # harvest path - otherwise the reward is collectable without killing its
    # goblins (and is a buy-low/harvest-high exploit).
    m["coins"] = 0
    viking = 490  # Viking Festival - spawns goblins that must be cleared
    m["items"].append([viking, 62, 62, 0, 0, 0])
    _do(Constant.CMD_SELL, [62, 62, viking, 0, 0, Constant.SELL_REASON_TREASURE])
    assert m["coins"] < Constant.TREASURE_DECORATION_GOLD, \
        "enemy-guarded festival paid treasure gold without a fight"


def test_bank_exchanges_gold_for_cash(tmp):
    # The Bank window's EXCHANGE button (repurposed direction): 15,000 gold ->
    # 1 cash. Without enough gold the command is rejected outright, so a
    # forged exchange_cash_new can't mint cash.
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 14999
    _do(Constant.CMD_EXCHANGE_CASH, [0])
    assert save["playerInfo"]["cash"] == 0, "exchange minted cash without gold"
    assert m["coins"] == 14999, "rejected exchange still took gold"

    m["coins"] = 31000
    _do(Constant.CMD_EXCHANGE_CASH, [0])
    assert save["playerInfo"]["cash"] == 1 and m["coins"] == 16000
    _do(Constant.CMD_EXCHANGE_CASH, [0])
    assert save["playerInfo"]["cash"] == 2 and m["coins"] == 1000


def test_collect_treasure_clamped_and_persisted(tmp):
    save = sessions.session(UID)
    m = save["maps"][0]
    xp_before = int(m["xp"])
    _do(Constant.CMD_COLLECT_TREASURE, [999999, 999999, 3, 0, 0, 0])
    assert m["coins"] == 1500, f"gold not clamped: {m['coins']}"
    assert int(m["xp"]) == xp_before + 200, f"xp not clamped: {m['xp']}"
    assert m["idCurrentTreasure"] == 3, "next quest id not persisted"


def test_kill_rewards_persist_for_units_and_towers(tmp):
    # The client shows +5 gold (+collect_xp xp) for a killed enemy unit and
    # +5 gold (+ceil(floor(cost/4)*0.02) xp) for a destroyed attacking stone
    # tower, but the server only recorded unit xp - the gold (and tower xp)
    # vanished on the next reload.
    save = sessions.session(UID)
    m = save["maps"][0]
    troll, tower = 525, 29  # Small Troll (collect_xp 1), Tower I (cost 125)
    m["items"].extend([
        [troll, 70, 70, 0, 0, 0],
        [tower, 71, 71, 0, 0, 0],
    ])
    gold0, xp0 = int(m["coins"]), int(m["xp"])

    _do(Constant.CMD_KILL, [70, 70, troll, 0, "u"])
    assert int(m["coins"]) == gold0 + 5, "unit kill gold not credited"
    assert int(m["xp"]) == xp0 + 1, "unit kill xp not credited"

    _do(Constant.CMD_KILL, [71, 71, tower, 0, "b"])
    # floor(125/4)=31 -> ceil(31*0.02)=1
    assert int(m["coins"]) == gold0 + 10, "tower kill gold not credited"
    assert int(m["xp"]) == xp0 + 2, "tower kill xp not credited"
    assert not any(it[1:3] in ([70, 70], [71, 71]) for it in m["items"]), \
        "killed items not removed"


def test_kill_reward_persists_when_enemy_moved(tmp):
    # Camp trolls wander in combat, so the client's reported kill tile no
    # longer matches the spawn tile stored at CMD_BUY. The kill must still
    # credit gold/xp and remove the enemy - otherwise the "killed" troll
    # respawns on reload and its gold vanishes.
    save = sessions.session(UID)
    m = save["maps"][0]
    troll = 525  # Small Troll (collect_xp 1)
    m["items"].append([troll, 70, 70, 0, 0, 0])
    gold0, xp0 = int(m["coins"]), int(m["xp"])
    n0 = sum(1 for it in m["items"] if it[0] == troll)
    # Report the kill at a DIFFERENT tile than where it was placed.
    _do(Constant.CMD_KILL, [88, 91, troll, 0, "u"])
    assert int(m["coins"]) == gold0 + 5, "moved-enemy kill gold not credited"
    assert int(m["xp"]) == xp0 + 1, "moved-enemy kill xp not credited"
    assert sum(1 for it in m["items"] if it[0] == troll) == n0 - 1, \
        "moved enemy not removed -> respawns on reload"


def test_kill_reward_credited_for_client_only_enemy(tmp):
    # Reinforcements / boss adds are spawned client-side and never CMD_BUY'd,
    # so no server item exists to match. The client still shows +5 gold for the
    # kill; the server must credit it too or the gold vanishes on reload.
    save = sessions.session(UID)
    m = save["maps"][0]
    boss_add = 547  # a Troll unit id with no matching stored item here
    m["items"] = [it for it in m["items"] if it[0] != boss_add]  # ensure none
    gold0, xp0 = int(m["coins"]), int(m["xp"])
    _do(Constant.CMD_KILL, [40, 40, boss_add, 0, "u"])
    assert int(m["coins"]) == gold0 + 5, \
        "client-only enemy kill gold not credited -> vanishes on reload"
    assert int(m["xp"]) >= xp0, "xp went backwards on client-only kill"


def test_battle_deaths_are_kept_in_the_graveyard(tmp):
    # Units that die vs trolls (quest) or players must land in the Graveyard so
    # they can be resurrected. Counted array row = [id, initial, killed,
    # recovered]; permanent losses = killed - recovered.
    save = sessions.session(UID)
    soldier = 535   # Ranger (stored)
    peasant = 500   # peasant - never stored
    payload = {
        "map": 0, "resources": {"g": 0, "x": 0},
        "units": [[soldier, 3, 2, 1], [peasant, 1, 1, 0]],
        "win": 1, "duration": 10, "voluntary_end": 0,
        "quest_id": 100000006, "difficulty": 1,
    }
    _do(Constant.CMD_END_QUEST, [json.dumps(payload)])
    grave = save["privateState"].get("deadHeroes")
    assert isinstance(grave, dict), "graveyard was not created"
    assert grave.get(str(soldier)) == 1, "battle death not kept in graveyard"
    assert str(peasant) not in grave, "peasant wrongly stored in graveyard"


def test_pvp_attacker_casualties_go_to_graveyard(tmp):
    save = sessions.session(UID)
    soldier = 535
    payload = {
        "attacker": {"name": "me", "level": 1, "user_id": UID,
                     "world_id": 0, "map": 0, "race": "h"},
        "victim": {}, "resources": {"g": 0, "x": 0},
        "resources_victim": {"g": 0},
        "attacker_units": [[soldier, 2, 2, 0]],  # both killed, none recovered
        "victim_units": [], "win": 1, "duration": 10,
        "voluntary_end": 0, "townhall_gold": 0, "honor": 0,
    }
    _do(Constant.CMD_END_ATTACK, [json.dumps(payload)])
    grave = save["privateState"].get("deadHeroes")
    assert isinstance(grave, dict) and grave.get(str(soldier)) == 2, \
        "PvP attacker casualties not kept in graveyard"


def test_resurrect_consumes_from_graveyard(tmp):
    save = sessions.session(UID)
    m = save["maps"][0]
    save["privateState"]["deadHeroes"] = {str(RANGER): 2}
    m["coins"] = 1500  # gold-path resurrect (cost_unit_cash 3 * 500)
    _do(Constant.CMD_RESURRECT_HERO, [RANGER, 5, 5, 0])
    assert save["privateState"]["deadHeroes"].get(str(RANGER)) == 1, \
        "resurrect did not consume the unit from the graveyard"


def _end_quest(quest_id, difficulty, gold, xp, win=True, town=0):
    payload = {
        "map": town,
        "resources": {"g": gold, "x": xp},
        "units": [],
        "win": 1 if win else 0,
        "duration": 10,
        "voluntary_end": 0,
        "quest_id": quest_id,
        "difficulty": difficulty,
    }
    _do(Constant.CMD_END_QUEST, [json.dumps(payload)])


def test_quest_reward_paid_once_per_difficulty(tmp):
    # A prize is granted per difficulty level (progressive), first clear only.
    # Replaying a difficulty already cleared pays nothing; a new, harder
    # difficulty pays again.
    save = sessions.session(UID)
    m = save["maps"][0]
    quest = 100000006
    base = int(m["coins"])

    _end_quest(quest, 1, 100, 10)          # first clear of difficulty 1
    assert int(m["coins"]) == base + 100, "difficulty 1 prize not paid"

    _end_quest(quest, 1, 100, 10)          # replay difficulty 1 - no prize
    assert int(m["coins"]) == base + 100, "difficulty 1 paid twice"

    _end_quest(quest, 2, 250, 25)          # first clear of difficulty 2
    assert int(m["coins"]) == base + 350, "difficulty 2 prize not paid"

    _end_quest(quest, 3, 500, 50)          # first clear of difficulty 3
    assert int(m["coins"]) == base + 850, "difficulty 3 prize not paid"

    _end_quest(quest, 3, 500, 50)          # replay difficulty 3 - no prize
    assert int(m["coins"]) == base + 850, "difficulty 3 paid twice"

    # A loss never pays, even at a fresh difficulty.
    other = 100000007
    snap = int(m["coins"])
    _end_quest(other, 1, 100, 10, win=False)
    assert int(m["coins"]) == snap, "loss paid a prize"


def test_survival_end_stores_prizes(tmp):
    # The survival arena gives its prizes to the client's warehouse locally but
    # only persists them via end_survival; with no handler the prizes vanished
    # on reload (arena "gave no reward").
    save = sessions.session(UID)
    m = save["maps"][0]
    m["store"] = {}
    _do(Constant.CMD_END_SURVIVAL, ["100000035", 300, json.dumps({"535": 2, "531": 1})])
    assert m["store"].get("535") == 2, "survival prize (x2) not stored"
    assert m["store"].get("531") == 1, "survival prize (x1) not stored"
    # A malformed / empty payload must not crash.
    _do(Constant.CMD_END_SURVIVAL, ["100000035", 0, ""])
    assert m["store"].get("535") == 2, "empty survival payload mutated storage"


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


# --- buy second town -------------------------------------------------------

def test_buy_map_creates_second_town_with_gold(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 50
    save["maps"][0]["coins"] = 150000
    _do(Constant.CMD_BUY_MAP, [1, 0, "t", 0])  # gold-paid troll town
    assert len(save["maps"]) == 2, "second town not created"
    assert save["maps"][0]["coins"] == 50000, f"gold price not charged: {save['maps'][0]['coins']}"
    assert save["maps"][1]["race"] == "t", "second town race wrong"
    town2_ids = [it[0] for it in save["maps"][1]["items"]]
    assert 289 in town2_ids, "troll town missing Troll Hall (289) - would train villagers"
    assert 26 not in town2_ids, "troll town still has the human Town Hall (26)"
    assert 512 not in town2_ids and 516 not in town2_ids, "troll town kept human starter units"
    assert len(save["playerInfo"]["map_names"]) == 2, "town name not registered"
    assert len(save["playerInfo"]["map_sizes"]) == 2, "town size not registered"


def test_buy_map_with_cash(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 50
    save["playerInfo"]["cash"] = 30
    _do(Constant.CMD_BUY_MAP, [1, 1, "t", 0])  # cash-paid
    assert len(save["maps"]) == 2, "second town not created (cash)"
    assert save["playerInfo"]["cash"] == 8, f"cash price not charged: {save['playerInfo']['cash']}"


def test_buy_map_rejects_second_purchase(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 50
    save["maps"][0]["coins"] = 500000
    _do(Constant.CMD_BUY_MAP, [1, 0, "t", 0])
    coins_after_first = save["maps"][0]["coins"]
    _do(Constant.CMD_BUY_MAP, [1, 0, "t", 0])  # already have two towns
    assert len(save["maps"]) == 2, "third town created"
    assert save["maps"][0]["coins"] == coins_after_first, "second purchase charged again"


def test_buy_map_troll_needs_level_20(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 10
    save["maps"][0]["coins"] = 500000
    _do(Constant.CMD_BUY_MAP, [1, 0, "t", 0])  # too low level for troll
    assert len(save["maps"]) == 1, "troll town created below level 20"
    assert save["maps"][0]["coins"] == 500000, "gold charged on rejected troll town"


def test_buy_map_insufficient_gold_rejected(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 50
    save["maps"][0]["coins"] = 500
    _do(Constant.CMD_BUY_MAP, [1, 0, "t", 0])
    assert len(save["maps"]) == 1, "town created without paying"
    assert save["maps"][0]["coins"] == 500, "gold changed on rejected purchase"


def test_load_repairs_broken_troll_town(tmp):
    # A troll town left with the human town hall (from the first buggy
    # buy_map) is rebuilt on load so it trains goblins, not villagers.
    save = sessions.session(UID)
    save["maps"].append({
        "race": "t", "coins": 0, "wood": 0, "stone": 0, "food": 0,
        "level": 1, "xp": 0, "expansions": [13],
        "items": [[26, 52, 52, 0, 0, 0], [512, 50, 42, 0, 0, 0]],  # human hall + unit
    })
    changed = sessions._repair_broken_troll_towns(save)
    assert changed, "broken troll town not detected"
    ids = [it[0] for it in save["maps"][1]["items"]]
    assert 289 in ids and 26 not in ids, "troll town not rebuilt with Troll Hall"
    # a healthy troll town is left alone
    assert not sessions._repair_broken_troll_towns(save), "rebuilt town wrongly re-repaired"


# --- neighbour aliasing ------------------------------------------------------

def test_save_info_level_derived_from_xp(tmp):
    # The village list must show the real level: derive it from xp like the
    # in-game HUD, not from the stored `level` field which drifts out of sync.
    save = sessions.session(UID)
    save["maps"][0]["xp"] = 9497483   # a level-99 xp total
    save["maps"][0]["level"] = 20     # stale/wrong stored field
    assert sessions.save_info(UID)["level"] == 99, "village list did not derive level from xp"


def test_static_scenarios_are_not_social_players(tmp):
    arthur = str(Constant.NEIGHBOUR_ARTHUR_GUINEVERE_1)
    assert sessions.neighbor_session(arthur) is not None, \
        "Arthur scenario is unavailable for its scripted visit"
    assert not sessions.is_friend(UID, arthur)
    assert not any(
        str(entry["pid"]) == arthur for entry in sessions.neighbors(UID)
    ), "scripted Arthur scenario leaked into the social neighbour list"
    assert sessions.neighbor_session("1111") is None, \
        "removed AcidCaos sample account is still loaded at runtime"
    assert all(
        str(entry["user_id"]) != "1111" for entry in sessions.pvp_profiles()
    ), "removed AcidCaos sample account is still a PvP opponent"


def test_collect_gated_on_opened_social_mine(tmp):
    # Stone Mine (id 16) is a social building requiring Geologist + Miner.
    # It must not produce until it is opened (attrs.si == None). A reload that
    # dropped the client-side staffing overlay must not let it be collected.
    save = sessions.session(UID)
    m = save["maps"][0]
    m["stone"] = 0
    # Placed but only partially staffed (0/2): si is an incomplete list.
    m["items"].append([16, 10, 10, 0, 1, 0, [], {"si": []}])
    command.do_command(UID, Constant.CMD_COLLECT, [10, 10, 0, 16, 0, 1])
    assert m["stone"] == 0, "unopened social mine produced stone (reload bypass)"
    # Once opened, collection works.
    m["items"][-1][7]["si"] = None
    command.do_command(UID, Constant.CMD_COLLECT, [10, 10, 0, 16, 0, 1])
    assert m["stone"] == 175, f"opened mine did not produce base stone, got {m['stone']}"


def test_market_trade_requires_open_market(tmp):
    # Market I (id 23) needs Butcher/Fishmonger/Greengrocer before it opens.
    # Trading must be refused until the market is staffed & opened (si == None),
    # so a reload cannot let an unstaffed market trade.
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 100000
    m["wood"] = 0
    # Unstaffed Market I present: trade must be refused.
    m["items"].append([23, 20, 20, 0, 1, 0, [], {"si": []}])
    ok = command.do_command(UID, Constant.CMD_TRADE_RESOURCE, [0, "w", 0, 100])
    assert ok is False, "trade allowed with an unstaffed market"
    assert m["wood"] == 0, "wood bought through an unstaffed market"
    # Open it: trade now works.
    m["items"][-1][7]["si"] = None
    ok = command.do_command(UID, Constant.CMD_TRADE_RESOURCE, [0, "w", 0, 100])
    assert ok is True and m["wood"] == 100, f"opened market trade failed (wood={m['wood']})"


def test_player_info_gates_natural_resource_reload_population(tmp):
    # The initial environment must populate, but established towns must carry
    # the client-side guard. Otherwise each browser reload instantly replaces
    # harvested trees before their persisted cooldown.
    from get_player_info import get_player_info
    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    save = sessions.session(UID)
    save["playerInfo"]["completed_tutorial"] = 0
    save["maps"][0]["naturalResourcesInitialized"] = 1
    # This case models a healthy established map. Empty legacy maps without
    # this migration marker are intentionally reopened once by the recovery
    # test in test_gameplay_state.py.
    save["maps"][0]["naturalResourceRecoveryVersion"] = 1
    save["privateState"].setdefault("arrayAnimals", {})
    get_player_info(UID, 0)
    assert marker not in save["privateState"]["arrayAnimals"], \
        "initial spawn suppressed during tutorial (arrow points at nothing)"
    # Once established, reload population is blocked; individual mineral/tree
    # cooldowns are restored separately by the server.
    save["playerInfo"]["completed_tutorial"] = 1
    get_player_info(UID, 0)
    assert save["privateState"]["arrayAnimals"].get(marker) == 1, \
        "established town can repopulate resources merely by reloading"


def test_assist_neighbour_grants_reward(tmp):
    # Assisting a neighbour credits ASSIST_REWARD_GOLD + ASSIST_REWARD_XP; the
    # command must persist it (client applies it locally). Batched _new scales
    # with the number of helped buildings. assist_receive is acknowledged only.
    from get_game_config import get_game_config
    g = get_game_config()["globals"]
    gold = int(g.get("ASSIST_REWARD_GOLD", 10))
    xp = int(g.get("ASSIST_REWARD_XP", 3))
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 0
    m["xp"] = 0
    assert command.do_command(UID, Constant.CMD_ASSIST_NEIGHBOUR, ["9999", 1, 0]) is True
    assert m["coins"] == gold and m["xp"] == xp, "single assist reward not granted"
    clicks = json.dumps([[1, 1, 5], [2, 2, 6], [3, 3, 7]])  # 3 buildings
    assert command.do_command(UID, Constant.CMD_ASSIST_NEIGHBOUR_NEW, ["9999", 0, clicks]) is True
    assert m["coins"] == gold * 4 and m["xp"] == xp * 4, "batched assist reward wrong"
    assert command.do_command(UID, Constant.CMD_ASSIST_RECEIVE, [0, 224]) is True
    assert m["coins"] == gold * 4, "assist_receive should not grant an unknown amount"


def test_place_gift_uses_town_arg_not_frame(tmp):
    # Client layout for both place_gift and place_stored_item is
    # [id, x, y, frame, townID]. place_gift wrongly read town from args[3]
    # (the frame), so a non-zero frame placed the gift in a wrong/out-of-range
    # town. Town must come from args[4].
    save = sessions.session(UID)
    save["privateState"]["gifts"] = [0] * RANGER + [1]
    m0 = save["maps"][0]
    n = len(m0["items"])
    command.do_command(UID, Constant.CMD_PLACE_GIFT, [RANGER, 12, 12, 5, 0])
    assert len(m0["items"]) == n + 1, "gift not placed in town 0 (frame misread as town)"
    assert m0["items"][-1][0] == RANGER, "wrong item placed"


def test_market_II_needs_no_staff(tmp):
    # Market II (id 188) is not a social building: it opens without helpers.
    save = sessions.session(UID)
    m = save["maps"][0]
    m["coins"] = 100000
    m["wood"] = 0
    m["items"].append([188, 30, 30, 0, 1, 0])
    ok = command.do_command(UID, Constant.CMD_TRADE_RESOURCE, [0, "w", 0, 100])
    assert ok is True and m["wood"] == 100, "non-social Market II should trade freely"


def test_loading_scrubs_leaked_playerinfo_fields(tmp):
    polluted = _template_save()
    polluted["playerInfo"]["coins"] = 123
    polluted["playerInfo"]["wood"] = 9
    json.dump(polluted, open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    save = sessions.session(UID)
    assert "coins" not in save["playerInfo"], "leaked coins not scrubbed on load"
    assert "wood" not in save["playerInfo"], "leaked wood not scrubbed on load"


def test_mission_reward_is_paid_only_once_after_completion(tmp):
    save = sessions.session(UID)
    mission_id = 6
    reward = int(get_game_config()["missions"][mission_id - 2]["reward"])
    before = int(save["maps"][0]["coins"])

    assert command.do_command(
        UID, Constant.CMD_REWARD_MISSION, [0, mission_id]
    ) is False, "an unfinished mission paid a reward"
    assert command.do_command(
        UID, Constant.CMD_COMPLETE_MISSION, [mission_id, 0]
    ) is True
    assert command.do_command(
        UID, Constant.CMD_REWARD_MISSION, [0, mission_id]
    ) is True
    assert command.do_command(
        UID, Constant.CMD_REWARD_MISSION, [0, mission_id]
    ) is False, "mission reward was redeemable twice"
    assert int(save["maps"][0]["coins"]) - before == reward
    assert save["privateState"]["completedMissions"].count(mission_id) == 1
    assert save["privateState"]["rewardedMissions"].count(mission_id) == 1


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
    test_allies_building_opens_once_and_never_re_asks,
    test_festival_decoration_harvest_pays_gold_but_not_quest_treasure,
    test_bank_exchanges_gold_for_cash,
    test_kill_rewards_persist_for_units_and_towers,
    test_collect_treasure_stamps_kill_time,
    test_buy_unit_with_cash,
    test_buy_map_creates_second_town_with_gold,
    test_buy_map_with_cash,
    test_buy_map_rejects_second_purchase,
    test_buy_map_troll_needs_level_20,
    test_buy_map_insufficient_gold_rejected,
    test_load_repairs_broken_troll_town,
    test_save_info_level_derived_from_xp,
    test_static_scenarios_are_not_social_players,
    test_collect_gated_on_opened_social_mine,
    test_player_info_gates_natural_resource_reload_population,
    test_assist_neighbour_grants_reward,
    test_place_gift_uses_town_arg_not_frame,
    test_market_trade_requires_open_market,
    test_market_II_needs_no_staff,
    test_loading_scrubs_leaked_playerinfo_fields,
    test_mission_reward_is_paid_only_once_after_completion,
    test_kill_reward_persists_when_enemy_moved,
    test_kill_reward_credited_for_client_only_enemy,
    test_battle_deaths_are_kept_in_the_graveyard,
    test_pvp_attacker_casualties_go_to_graveyard,
    test_resurrect_consumes_from_graveyard,
    test_quest_reward_paid_once_per_difficulty,
    test_survival_end_stores_prizes,
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
