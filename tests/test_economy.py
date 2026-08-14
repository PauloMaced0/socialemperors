"""Economy tests: level-up rewards (GD-04) and daily bonus (GD-05).

    /path/to/.venv/bin/python tests/test_economy.py

Plain asserts, isolated temp saves dir; never touches ./saves.
"""
import os
import sys
import json
import shutil
import tempfile
import calendar
import datetime

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
    import get_player_info as gpi
    gpi.timestamp_now = lambda: t


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


def test_levelup_mana_reward_survives_reload(tmp):
    save = sessions.session(UID)
    save["maps"][0]["level"] = 35
    save["privateState"]["mana"] = 7

    _do(Constant.CMD_RT_LEVEL_UP, [36])
    assert save["privateState"]["mana"] == 9, \
        "level 36 did not persist the configured +2 mana"
    sessions.save_session(UID)
    sessions.load_saved_villages()
    reloaded = sessions.session(UID)
    assert reloaded["privateState"]["mana"] == 9, \
        "level-up mana disappeared after reload"

    _do(Constant.CMD_RT_LEVEL_UP, [36])
    assert reloaded["privateState"]["mana"] == 9, \
        "replayed level-up duplicated mana"
    _do(Constant.CMD_RT_LEVEL_UP, [38])
    assert reloaded["privateState"]["mana"] == 13, \
        "multi-level jump did not grant mana for both crossed levels"


# --- Building upgrades and resource progression --------------------------
# The Flash client upgrades in a two-command batch:
#   sell(old, reason="UPGR"), buy(next, multiplier=1)
# It charges the next tier's full configured price and gives the ordinary 5%
# resale credit for the old tier. It does not merely charge the price
# difference between the two tiers.

def test_upgrade_charges_next_tier_less_resale_credit(tmp):
    save = sessions.session(UID)
    save["maps"][0]["wood"] = 100
    _do(Constant.CMD_SELL, [44, 50, 1, 0, 0, Constant.SELL_REASON_UPGRADE])
    _do(Constant.CMD_BUY, [2, 44, 50, 1, 0, 0, 1, "b"])

    town = save["maps"][0]
    assert town["wood"] == 41, \
        f"House I -> II should charge 60 wood minus a 1-wood resale credit, got {100 - town['wood']}"
    assert any(item[0] == 2 and item[1:3] == [44, 50] for item in town["items"]), \
        "upgraded House II was not persisted"
    assert not any(item[0] == 1 and item[1:3] == [44, 50] for item in town["items"]), \
        "old House I remained after upgrade"


def test_normal_sale_still_refunds_five_percent(tmp):
    save = sessions.session(UID)
    save["maps"][0]["wood"] = 0
    _do(Constant.CMD_SELL, [44, 50, 1, 0, 0, "SELL"])
    # int(30 * 0.05) truncates to one wood, matching the client/server rule.
    assert save["maps"][0]["wood"] == 1, \
        "ordinary sale lost its configured 5% refund"


def test_resource_upgrade_chains_are_monotonic(tmp):
    items = {int(item["id"]): item for item in get_game_config()["items"]}
    crop_chain = [10, 8, 9, 200, 201]
    for lower, higher in zip(crop_chain, crop_chain[1:]):
        assert int(items[lower]["upgrades_to"]) == higher
    assert [int(items[item_id]["min_level"]) for item_id in crop_chain] == \
        sorted(int(items[item_id]["min_level"]) for item_id in crop_chain), \
        "crop upgrade chain goes backwards in level"
    assert [int(items[item_id]["collect"]) for item_id in crop_chain] == \
        sorted(int(items[item_id]["collect"]) for item_id in crop_chain), \
        "crop upgrade chain lowers food yield"
    assert int(items[301]["cost"]) == 200 and items[301]["cost_type"] == "w", \
        "Troll Mill II is still a free upgrade"


def test_great_church_grants_population(tmp):
    # Great Church (id 470) shipped with population=10; a config patch raises
    # it to a meaningful housing boost. The plain Church (24) stays 0.
    items = {int(item["id"]): item for item in get_game_config()["items"]
             if str(item.get("name") or "")}
    great = next(it for it in get_game_config()["items"]
                 if str(it.get("id")) == "470" and it.get("name") == "Great Church")
    assert int(great["population"]) == 50, \
        "Great Church population boost not applied"


def test_training_a_unit_also_charges_food(tmp):
    # IsoBuilding pays the unit's `cost` in its own cost_type AND
    # ceil(cost * Config.FOOD_PER_GOLD_INTRAINING) food. The server used to
    # charge only the cost_type, so a reload gave the food straight back.
    town = sessions.session(UID)["maps"][0]
    town["coins"] = 1000
    town["food"] = 1000
    town["items"].append([38, 60, 60, 0, 0, 0])  # Barracks I, trains Spearman
    _do(Constant.CMD_BUY, [509, 61, 61, 0, 0, 0, 1, "u", 60, 60, 38])

    assert town["coins"] == 970, \
        f"Spearman gold cost wrong: {1000 - town['coins']}"
    assert town["food"] == 940, \
        f"Spearman food cost should be 2x its 30 gold: {1000 - town['food']}"


def test_training_a_peasant_charges_no_extra_food(tmp):
    # SUBCATFUNC_UNIT_PEASANT is exempt from the food surcharge; a Villager
    # costs exactly its configured 50 food.
    town = sessions.session(UID)["maps"][0]
    town["food"] = 1000
    town["items"].append([26, 62, 62, 0, 0, 0])  # Town Hall, trains Villager
    _do(Constant.CMD_BUY, [500, 63, 63, 0, 0, 0, 1, "u", 62, 62, 26])

    assert town["food"] == 950, \
        f"Villager should cost 50 food and nothing more: {1000 - town['food']}"


def test_dragon_rider_buildings_have_store_descriptions(tmp):
    literals = {
        int(entry["id"]): entry["text"]
        for entry in get_game_config()["localization_strings"]
        if isinstance(entry, dict)
    }
    assert "Train dragon riders" in literals[1288], \
        "Rider Academy store description is still blank"
    assert "compatible dragon" in literals[1289], \
        "Dragon Riding store description is still blank"


def test_training_stables_has_a_real_production_cycle(tmp):
    # Item 227 shipped with activation=0 and collect=0 in every config dump.
    # The client computes the progress bar as (serverTime - item[4]) / activation,
    # so activation=0 is a divide-by-zero: the bar sticks at 0% and the training
    # cycle loops forever; collect=0 means it also earns nothing. A config patch
    # restores a real cycle. Values are tunable; they must simply be nonzero.
    items = {int(item["id"]): item for item in get_game_config()["items"]}
    stables = items[Constant.ID_BUILDING_STABLE_TRAINING]
    assert int(stables["activation"]) > 0, \
        "Training Stables activation=0 -> 0% progress, infinite training loop"
    assert int(stables["collect"]) > 0, \
        "Training Stables collect=0 -> Earns 0 forever"


def test_upgraded_social_buildings_list_inherited_roles_first(tmp):
    social = {
        int(item["id"]): [
            role.strip()
            for role in str(item.get("workers", "")).split(",")
            if role.strip()
        ]
        for item in get_game_config()["social_items"]
    }
    assert social[18][:2] == ["Geologist", "Miner"], \
        "Stone Mine III no longer maps inherited level-I staff by role"
    assert social[18][2:] == ["Cartographer", "Engineer", "Supervisor"], \
        "Stone Mine III did not leave exactly its three new jobs vacant"
    assert social[189][:3] == social[23], \
        "Market III no longer carries Market I's three staff roles"
    assert social[50] == social[49], \
        "Workshop III changed the already-filled Workshop II role set"


# --- GD-05: daily bonus ---------------------------------------------------
# The client (PopupNewDaily/Utils.isDailyBonusReady) gates the popup by UTC
# CALENDAR DAY and displays reward index (bonusNextId - 1) % 5. The server
# must mirror both or the reward shown on screen differs from the one saved.

def _utc(day, hour):
    "Epoch seconds for 1970-01-<day> <hour>:00 UTC."
    return calendar.timegm((1970, 1, day, hour, 0, 0))


def _local(day, hour):
    "Epoch seconds for 1970-01-<day> <hour>:00 LOCAL time (darts use local days)."
    return int(datetime.datetime(1970, 1, day, hour, 0).timestamp())


def test_daily_bonus_uses_config_not_client(tmp):
    _now(_utc(12, 15))
    # client tries to inject 999 cash / 999 gold
    _do(Constant.CMD_WIN_BONUS, [999, 0, 0, 0, 999])
    save = sessions.session(UID)
    # config[0] = 250 gold; client resource values must be ignored
    assert save["playerInfo"]["cash"] == 0, f"client cash injected: {save['playerInfo']['cash']}"
    assert save["maps"][0]["coins"] == 250, f"expected 250 gold from config, got {save['maps'][0]['coins']}"
    # client shows (bonusNextId - 1) % 5 next login, so day-2 needs id 2
    assert save["privateState"]["bonusNextId"] == 2, "bonusNextId must advance to 2 (client displays id-1)"


def test_daily_bonus_cooldown_blocks_second_claim(tmp):
    _now(_utc(12, 10))
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])          # claim #1 -> 250 gold
    coins1 = sessions.session(UID)["maps"][0]["coins"]
    _now(_utc(12, 23))                                     # same UTC day
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 1, 999])         # attempt #2
    save = sessions.session(UID)
    assert save["maps"][0]["coins"] == coins1, "second same-day claim was NOT blocked"
    assert save["playerInfo"]["cash"] == 0, "second claim leaked cash"
    assert save["privateState"]["bonusNextId"] == 2, "blocked claim advanced streak"


def test_daily_bonus_next_day_gives_next_reward(tmp):
    heroes = get_game_config()["globals"]["DAILY_BONUS_CONFIG_HEROES"]
    hero_id = int(heroes[3])
    _now(_utc(12, 20))
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])           # #1 -> 250 gold
    # next UTC day only 8h later: client offers the bonus again (calendar
    # day), server must accept even though < 24h elapsed
    _now(_utc(13, 4))
    _do(Constant.CMD_WIN_BONUS, [0, 0, hero_id, 0, 0])     # #2 -> config[1] = hero
    gifts = sessions.session(UID)["privateState"]["gifts"]
    assert len(gifts) > hero_id and gifts[hero_id] >= 1, "day-2 hero bonus not granted"
    assert sessions.session(UID)["privateState"]["bonusNextId"] == 3, "streak not advanced to 3"


def test_daily_bonus_grants_the_hero_the_client_showed(tmp):
    heroes = get_game_config()["globals"]["DAILY_BONUS_CONFIG_HEROES"]
    shown = int(heroes[-1])  # client picks a RANDOM hero and displays it
    save = sessions.session(UID)
    save["privateState"]["bonusNextId"] = 2  # today displays index 1 = hero day
    save["privateState"]["timestampLastBonus"] = _utc(11, 12)
    _now(_utc(12, 12))
    _do(Constant.CMD_WIN_BONUS, [0, 0, shown, 2, 0])
    gifts = save["privateState"]["gifts"]
    assert len(gifts) > shown and gifts[shown] >= 1, "server stored a different hero than the client showed"


def test_daily_bonus_hero_outside_config_rejected(tmp):
    save = sessions.session(UID)
    save["privateState"]["bonusNextId"] = 2  # hero day
    save["privateState"]["timestampLastBonus"] = _utc(11, 12)
    _now(_utc(12, 12))
    _do(Constant.CMD_WIN_BONUS, [0, 0, 1, 2, 0])  # hero 1 not in DAILY_BONUS_CONFIG_HEROES
    heroes = [int(h) for h in get_game_config()["globals"]["DAILY_BONUS_CONFIG_HEROES"]]
    gifts = save["privateState"]["gifts"]
    assert len(gifts) <= 1 or gifts[1] == 0, "arbitrary client hero id was stored"
    granted = [h for h in heroes if len(gifts) > h and gifts[h] >= 1]
    assert len(granted) == 1, f"expected fallback hero from config, got {granted}"


def test_daily_bonus_streak_resets_after_missed_day(tmp):
    _now(_utc(12, 12))
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])           # day 1 -> 250 gold
    coins1 = sessions.session(UID)["maps"][0]["coins"]
    _now(_utc(15, 12))                                     # skipped 2 days
    _do(Constant.CMD_WIN_BONUS, [0, 0, 0, 0, 0])           # back to day 1
    save = sessions.session(UID)
    assert save["maps"][0]["coins"] == coins1 + 250, "reset claim should grant day-1 gold again"
    assert save["privateState"]["bonusNextId"] == 2, "streak should restart at day 1"


# --- Darts: one free game per LOCAL calendar day, extras billed -----------
# Client flow (PopupDarts): on a new local day it sends darts_new_free, then
# darts_shoot_balloon. Extra throws on the same day are "Play again for 20"
# billed in cash. The board (dartsBalloonsShot) persists all week; only
# darts_reset (new weekly prize set) clears it.

def _seed_darts(save, cash=0):
    save["playerInfo"]["cash"] = cash
    ps = save["privateState"]
    ps["dartsBalloonsShot"] = []
    ps["dartsHasFree"] = False
    ps["timeStampDartsNewFree"] = 0
    ps["timeStampLastDart"] = 0
    return ps


def test_darts_free_then_billed_then_broke(tmp):
    _now(_local(12, 15))
    save = sessions.session(UID)
    ps = _seed_darts(save, cash=25)
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    assert ps["dartsHasFree"] is True, "free game claim rejected"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [1, 0, 0])   # free
    assert ps["dartsBalloonsShot"] == [1], "free throw not recorded"
    assert save["playerInfo"]["cash"] == 25, "free throw wrongly billed"
    assert ps["dartsHasFree"] is False, "free throw did not consume the free game"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 1, 0])   # extra -> billed 20
    assert ps["dartsBalloonsShot"] == [1, 2], "paid throw not recorded"
    assert save["playerInfo"]["cash"] == 5, f"extra throw not billed: {save['playerInfo']['cash']}"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [3, 1, 0])   # broke -> rejected
    assert ps["dartsBalloonsShot"] == [1, 2], "broke throw not rejected"
    assert save["playerInfo"]["cash"] == 5, "broke throw changed cash"


def test_darts_free_next_local_day_even_within_24h(tmp):
    # Yesterday's bug: threw at 20:00, next morning 08:00 (< 24h later) the
    # client offers the free game (new local day) but the server billed it.
    _now(_local(12, 20))
    save = sessions.session(UID)
    ps = _seed_darts(save, cash=0)
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [1, 0, 0])
    _now(_local(12, 23))
    _do(Constant.CMD_DARTS_NEW_FREE, [])               # same day -> rejected
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 1, 0])   # no cash -> rejected
    assert ps["dartsBalloonsShot"] == [1], "same-day free repeat leaked"
    _now(_local(13, 8))                                # next local day, 12h later
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    assert ps["dartsHasFree"] is True, "next-morning free game blocked (24h gate regression)"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [2, 0, 0])
    assert ps["dartsBalloonsShot"] == [1, 2], "next-day free throw blocked"
    assert save["playerInfo"]["cash"] == 0, "next-day free throw billed"


def test_darts_new_free_keeps_board(tmp):
    # dartsBalloonsShot is the WEEK's discovered prizes; a new daily free
    # game must not wipe it (only darts_reset on a new weekly set does).
    _now(_local(12, 15))
    save = sessions.session(UID)
    ps = _seed_darts(save)
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [7, 0, 0])
    _now(_local(13, 15))
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    assert ps["dartsBalloonsShot"] == [7], "new daily free game wiped the weekly board"


def test_darts_unused_free_survives_reload_same_day(tmp):
    # Claimed the free game, closed the popup without throwing, reloaded:
    # get_player_info must not revoke the unused free throw.
    import get_player_info as gpi
    _now(_local(12, 15))
    save = sessions.session(UID)
    ps = _seed_darts(save)
    _do(Constant.CMD_DARTS_NEW_FREE, [])
    gpi.get_player_info(UID)
    assert ps["dartsHasFree"] is True, "unused free game revoked on reload"
    _do(Constant.CMD_DARTS_SHOOT_BALLOON, [4, 0, 0])
    gpi.get_player_info(UID)
    assert ps["dartsHasFree"] is False, "consumed free game resurrected on reload"


TESTS = [
    test_levelup_grants_cash_at_level_5,
    test_levelup_is_idempotent,
    test_levelup_single_step_grants_only_that_level,
    test_levelup_mana_reward_survives_reload,
    test_upgrade_charges_next_tier_less_resale_credit,
    test_normal_sale_still_refunds_five_percent,
    test_resource_upgrade_chains_are_monotonic,
    test_great_church_grants_population,
    test_training_a_unit_also_charges_food,
    test_training_a_peasant_charges_no_extra_food,
    test_dragon_rider_buildings_have_store_descriptions,
    test_training_stables_has_a_real_production_cycle,
    test_upgraded_social_buildings_list_inherited_roles_first,
    test_daily_bonus_uses_config_not_client,
    test_daily_bonus_cooldown_blocks_second_claim,
    test_daily_bonus_next_day_gives_next_reward,
    test_daily_bonus_grants_the_hero_the_client_showed,
    test_daily_bonus_hero_outside_config_rejected,
    test_daily_bonus_streak_resets_after_missed_day,
    test_darts_free_then_billed_then_broke,
    test_darts_free_next_local_day_even_within_24h,
    test_darts_new_free_keeps_board,
    test_darts_unused_free_survives_reload_same_day,
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
