"""Regression tests for map population and long-running gameplay state.

Run from the repository root:

    .venv/bin/python tests/test_gameplay_state.py

Plain asserts and an isolated save directory; real player saves are untouched.
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command
import engine
import get_player_info
import sessions
from constants import Constant
from get_game_config import get_attribute_from_item_id


UID = "test-gameplay-state-0001"


def _local(day, hour):
    return int(datetime.datetime(1970, 1, day, hour, 0).timestamp())


def _set_now(value):
    command.timestamp_now = lambda: value
    engine.timestamp_now = lambda: value
    get_player_info.timestamp_now = lambda: value


def _template_save():
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = UID
    save["playerInfo"]["cash"] = 100
    return save


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="se_gameplay_")
    sessions.SAVES_DIR = tmp
    command.SAVES_DIR = tmp
    json.dump(
        _template_save(),
        open(os.path.join(tmp, f"{UID}.save.json"), "w"),
        indent=4,
    )
    sessions.load_saved_villages()
    return tmp


def _batch(commands):
    return {
        "ts": 1,
        "first_number": 1,
        "accessToken": "x",
        "tries": 0,
        "publishActions": [],
        "commands": commands,
    }


def _reload():
    sessions.load_saved_villages()
    return sessions.session(UID)


def _item(save, item_id, x, y):
    return next(
        item for item in save["maps"][0]["items"]
        if int(item[0]) == item_id and item[1] == x and item[2] == y
    )


def _add_saved_player(tmp, uid, name):
    save = _template_save()
    save["playerInfo"]["pid"] = uid
    save["playerInfo"]["map_names"] = [name]
    json.dump(
        save,
        open(os.path.join(tmp, f"{uid}.save.json"), "w"),
        indent=4,
    )


def test_animal_spawn_budget_resets_on_next_local_day(tmp):
    save = sessions.session(UID)
    state = save["privateState"]
    state["arrayAnimals"] = {"74": 20, "75": 4}
    state["timestampAnimalsReset"] = _local(12, 9)

    _set_now(_local(12, 18))
    get_player_info.get_player_info(UID)
    assert state["arrayAnimals"] == {"74": 20, "75": 4}, \
        "same-day reload reset the daily animal allowance"

    _set_now(_local(13, 8))
    get_player_info.get_player_info(UID)
    assert state["arrayAnimals"] == {}, \
        "next local day did not reset the animal replenishment allowance"


def test_dragon_nest_progress_survives_second_dragon_reload(tmp):
    _set_now(_local(12, 10))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_NEXT_DRAGON_STEP, "args": [0]},
    ]))
    first = _reload()["privateState"]
    assert first["stepNumber"] == 1
    assert first["timeStampTakeCare"] == _local(12, 10)

    command.command(UID, _batch([
        {"cmd": Constant.CMD_NEXT_DRAGON, "args": []},
        {"cmd": Constant.CMD_NEXT_DRAGON_STEP, "args": [0]},
    ]))
    second = _reload()["privateState"]
    assert second["dragonNumber"] == 1, "second dragon number was lost"
    assert second["stepNumber"] == 1, "second dragon care step was lost"
    assert second["timeStampTakeCare"] == _local(12, 10), \
        "second dragon timer start was lost"


def test_villager_assignment_and_work_timer_survive_reload(tmp):
    save = sessions.session(UID)
    mine_id, x, y = 16, 61, 61
    villager_id = 500
    save["maps"][0]["items"].extend([
        [mine_id, x, y, 0, 0, 0, [], {"si": None}],
        [villager_id, 58, 58, 0, 0, 0],
    ])
    _set_now(_local(12, 11))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_PUSH_UNIT,
         "args": [58, 58, villager_id, x, y, 0]},
        {"cmd": Constant.CMD_ACTIVATE,
         "args": [x, y, 0, mine_id, 2]},
    ]))

    reloaded = _reload()
    mine = _item(reloaded, mine_id, x, y)
    assert mine[6] == [villager_id], "assigned villager left the mine on refresh"
    assert mine[7]["cp"] == 2, "active work option stopped on refresh"
    assert mine[4] == _local(12, 11), "work timer restarted on refresh"
    assert not any(item[0] == villager_id for item in reloaded["maps"][0]["items"]), \
        "assigned villager was duplicated outside the mine"


def test_second_worker_yield_and_moving_producer_preserve_progress(tmp):
    save = sessions.session(UID)
    mine_id, x, y = 16, 63, 63
    # Option 3 is the client's 2x production duration/yield. The second
    # worker adds 20% of the base without shortening that timer.
    start = _local(12, 11)
    save["maps"][0]["items"].append([
        mine_id, x, y, 0, 0, 0, [500, 501], {"si": None},
    ])
    _set_now(start)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ACTIVATE,
         "args": [x, y, 0, mine_id, 3]},
    ]))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_MOVE,
         "args": [x, y, mine_id, x + 1, y + 1, 0, 0, "mouseUsed"]},
    ]))
    moved = _item(_reload(), mine_id, x + 1, y + 1)
    assert moved[4] == start and moved[6] == [500, 501]
    assert moved[7]["cp"] == 3, \
        "moving a mine/mill reset its workers or production progress"

    town = sessions.session(UID)["maps"][0]
    collect_type = get_attribute_from_item_id(mine_id, "collect_type")
    resource_key = {
        "g": "coins", "w": "wood", "s": "stone", "f": "food",
    }[collect_type]
    before = int(town[resource_key])
    base = int(get_attribute_from_item_id(mine_id, "collect"))
    expected = int((base + base * 0.2) * 2)
    _set_now(start + 4 * 60 * 60)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_COLLECT,
         "args": [x + 1, y + 1, 0, mine_id, 2, 1, 0]},
    ]))
    after = int(_reload()["maps"][0][resource_key])
    assert after - before == expected, \
        "the second worker/production option bonus vanished after sync"


def test_late_worker_bonus_is_prorated_and_client_count_is_ignored(tmp):
    save = sessions.session(UID)
    mine_id, x, y = 16, 64, 64
    first_worker, late_worker = 500, 501
    start = _local(12, 10)
    save["maps"][0]["items"].extend([
        [mine_id, x, y, 0, 0, 0, [first_worker], {"si": None}],
        [late_worker, 58, 58, 0, 0, 0],
    ])

    _set_now(start)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ACTIVATE,
         "args": [x, y, 0, mine_id, 2]},
    ]))

    # Add the second worker with only one minute left in a one-hour cycle.
    # The client-supplied worker count at collection is deliberately forged;
    # the server must use persisted worker-time instead.
    _set_now(start + 59 * 60)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_PUSH_UNIT,
         "args": [58, 58, late_worker, x, y, 0]},
    ]))
    town = sessions.session(UID)["maps"][0]
    collect_type = get_attribute_from_item_id(mine_id, "collect_type")
    resource_key = {
        "g": "coins", "w": "wood", "s": "stone", "f": "food",
    }[collect_type]
    base = int(get_attribute_from_item_id(mine_id, "collect"))
    before = int(town[resource_key])

    _set_now(start + 60 * 60)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_COLLECT,
         "args": [x, y, 0, mine_id, 999, 1, 0]},
    ]))
    after = int(_reload()["maps"][0][resource_key])
    average_workers = (59 * 60 + 2 * 60) / (60 * 60)
    prorated = int(base + (average_workers - 1) * base * 0.2)
    assert after - before == prorated
    assert after - before < int(base * 1.2), \
        "a last-minute worker incorrectly earned a full-cycle bonus"

    # Collection begins a new cycle with both assigned workers. They now
    # participate for the complete hour and earn the normal 20% bonus.
    next_start = start + 60 * 60
    _set_now(next_start + 60 * 60)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_COLLECT,
         "args": [x, y, 0, mine_id, 1, 1, 0]},
    ]))
    final = int(_reload()["maps"][0][resource_key])
    assert final - after == int(base * 1.2), \
        "the second worker did not earn its bonus after a full cycle"


def test_stone_mine_staffing_and_open_state_survive_reload(tmp):
    save = sessions.session(UID)
    mine_id, x, y = 16, 62, 62
    save["maps"][0]["items"].append([mine_id, x, y, 0, 0, 0])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, mine_id]},
        {"cmd": Constant.CMD_FINISH_SI, "args": [x, y, 0, mine_id]},
    ]))
    one_worker = _item(_reload(), mine_id, x, y)
    assert one_worker[7]["si"] == [0], \
        "Stone Mine opened with only Miner/Geologist instead of both roles"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, mine_id]},
    ]))
    staffed = _item(_reload(), mine_id, x, y)
    assert staffed[7]["si"] == [0, 0], "Geologist/Miner staffing was lost"
    assert sessions.session(UID)["playerInfo"]["cash"] == 96, \
        "two 2-cash workers were not charged"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_FINISH_SI, "args": [x, y, 0, mine_id]},
    ]))
    opened = _item(_reload(), mine_id, x, y)
    assert opened[7]["si"] is None, \
        "opened Stone Mine reverted to the staffing window after refresh"


def test_saved_players_are_not_automatic_neighbors(tmp):
    other = "test-gameplay-neighbor-0002"
    _add_saved_player(tmp, other, "Second Empire")
    sessions.load_saved_villages()

    visible = sessions.neighbors(UID)
    assert not any(str(entry["pid"]) == other for entry in visible), \
        "an unrelated save was automatically exposed as a neighbour"
    assert sessions.link_friend(UID, other)
    visible = sessions.neighbors(UID)
    assert any(str(entry["pid"]) == other for entry in visible), \
        "an explicitly linked local neighbour did not appear"
    assert UID in sessions.session(other)["privateState"]["neighbors"], \
        "friendship was not reciprocal"
    sessions.load_saved_villages()
    assert sessions.is_friend(UID, other), \
        "friendship disappeared after reload"


def test_invalid_market_values_and_xp_level_are_repaired_on_load(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    town["numTradesDone"] = "NaN"
    town["timestampLastTrade"] = "bad"
    town["resourcesTraded"] = []
    town["xp"] = 500
    town["level"] = 99
    response = get_player_info.get_player_info(UID)
    assert response["map"]["numTradesDone"] == 0
    assert response["map"]["timestampLastTrade"] == 0
    assert response["map"]["resourcesTraded"] == {}
    assert response["map"]["level"] == 6, \
        "HUD level was not normalized from XP before calculating the XP bar"


def test_quest_casualties_and_rescued_units_persist(tmp):
    town = sessions.session(UID)["maps"][0]
    town["items"] = [item for item in town["items"] if item[0] not in (512, 535)]
    town["items"].extend([
        [512, 40, 40, 0, 0, 0, [], {}],
        [512, 41, 40, 0, 0, 0, [], {}],
        [512, 42, 40, 0, 0, 0, [], {}],
    ])
    payload = json.dumps({
        "map": 0,
        "resources": {"g": 0, "x": 0},
        # Two died, one was recovered: exactly one permanent casualty.
        # The Ranger row is a quest rescue/reward.
        "units": [[512, 3, 2, 1], [535, 0, 0, 1]],
        "win": 1,
        "duration": 60,
        "voluntary_end": 0,
        "quest_id": 100000006,
        "difficulty": 1,
    })
    command.command(UID, _batch([
        {"cmd": Constant.CMD_END_QUEST, "args": [payload]},
    ]))
    reloaded = _reload()["maps"][0]["items"]
    assert sum(1 for item in reloaded if item[0] == 512) == 2, \
        "a non-recovered quest casualty returned to the village"
    assert sum(1 for item in reloaded if item[0] == 535) == 1, \
        "a rescued/free quest unit was not added to the village"


def test_pvp_history_limits_and_casualties_persist(tmp):
    defender_id = "test-gameplay-defender-0002"
    defender = _template_save()
    defender["playerInfo"]["pid"] = defender_id
    defender["playerInfo"]["map_names"] = ["Defender"]
    defender["maps"][0]["items"] = [
        item for item in defender["maps"][0]["items"] if item[0] != 516
    ]
    defender["maps"][0]["items"].append([516, 60, 60, 0, 0, 0, [], {}])
    json.dump(
        defender,
        open(os.path.join(tmp, f"{defender_id}.save.json"), "w"),
        indent=4,
    )
    sessions.load_saved_villages()
    _set_now(_local(12, 15))

    command.command(UID, _batch([
        {"cmd": Constant.CMD_ATTACK_PLAYER, "args": [defender_id]},
        {"cmd": Constant.CMD_END_ATTACK, "args": [json.dumps({
            "attacker": {"user_id": UID, "name": "My Empire", "map": 0},
            "victim": {
                "user_id": defender_id, "name": "Defender",
                "map": 0, "posicion": 2,
            },
            "resources": {"g": 10, "x": 2},
            "resources_victim": {"g": 0},
            "attacker_units": [],
            "victim_units": [[516, 1, 1, 0]],
            "win": 1,
            "honor": 3,
        })]},
    ]))

    attacker_state = sessions.session(UID)["privateState"]
    defender_state = sessions.session(defender_id)["privateState"]
    assert attacker_state["attacksWon"] == 1
    assert attacker_state["attacksSent"][-1].get("description")
    assert defender_state["attacksLost"] == 1
    assert defender_state["attacksReceived"][-1]["viewPending"] == 1
    assert not any(
        item[0] == 516 and item[1:3] == [60, 60]
        for item in sessions.session(defender_id)["maps"][0]["items"]
    ), "a non-recovered PvP casualty returned to the defender village"

    # Four-hour same-opponent cooldown is persisted and rejects a reload retry.
    before = len(attacker_state["attacksSent"])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ATTACK_PLAYER, "args": [defender_id]},
    ]))
    assert len(attacker_state["attacksSent"]) == before

    command.command(defender_id, _batch([
        {"cmd": Constant.CMD_CLEAN_ATTACKS, "args": []},
    ]))
    assert "viewPending" not in defender_state["attacksReceived"][-1]


def test_market_staffing_trade_and_allies_choice_persist(tmp):
    save = sessions.session(UID)
    market_id, x, y = 23, 63, 63
    allies_id, ax, ay = 266, 65, 65
    save["maps"][0]["items"].extend([
        [market_id, x, y, 0, 0, 0],
        [allies_id, ax, ay, 0, 0, 0],
    ])
    save["maps"][0]["coins"] = 1000
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, market_id]},
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, market_id]},
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, market_id]},
        {"cmd": Constant.CMD_FINISH_SI, "args": [x, y, 0, market_id]},
        {"cmd": Constant.CMD_TRADE_RESOURCE, "args": [0, "s", 0, 100]},
        {"cmd": Constant.CMD_SET_RESOURCE_ALLIES,
         "args": ["w", ax, ay, 0, allies_id]},
    ]))

    reloaded = _reload()
    market = _item(reloaded, market_id, x, y)
    town = reloaded["maps"][0]
    assert market[7]["si"] is None, "opened Market lost its staffing state"
    assert town["coins"] == 850 and town["stone"] == 350, \
        "Market buy did not persist the client's 150g-for-100-stone trade"
    assert town["numTradesDone"] == 1
    assert town["resourcesTraded"]["s"] == 1
    assert town["resourceAlliesMarket"] == "w", \
        "Allies Market resource choice was lost on refresh"


def test_hire_friends_staffs_social_buildings_and_persists(tmp):
    friend_id = "test-gameplay-friend-0002"
    _add_saved_player(tmp, friend_id, "Helper Empire")
    sessions.load_saved_villages()
    assert sessions.link_friend(UID, friend_id)

    recruitment_id = Constant.ID_BUILDING_ALLIES_RECRUITMENT
    table_id = Constant.ID_BUILDING_ROUND_TABLE
    rx, ry, tx, ty = 65, 65, 66, 66
    town = sessions.session(UID)["maps"][0]
    town["items"].extend([
        [recruitment_id, rx, ry, 0, 0, 0, [], {"si": []}],
        [table_id, tx, ty, 0, 0, 0, [], {"si": [], "sif": {}}],
    ])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_HIRE_WORKER,
         "args": [rx, ry, 0, recruitment_id, "does-not-exist"]},
        {"cmd": Constant.CMD_HIRE_WORKER,
         "args": [rx, ry, 0, recruitment_id, friend_id]},
        # Duplicate clicks cannot let the same friend occupy every role.
        {"cmd": Constant.CMD_HIRE_WORKER,
         "args": [rx, ry, 0, recruitment_id, friend_id]},
        {"cmd": Constant.CMD_ASSIST_SEND_FEED,
         "args": [tx, ty, 0, table_id, friend_id]},
    ]))

    reloaded = _reload()
    recruitment = _item(reloaded, recruitment_id, rx, ry)
    table = _item(reloaded, table_id, tx, ty)
    assert recruitment[7]["si"] == [friend_id]
    assert table[7]["si"] == [friend_id]
    assert table[7]["sif"] == {friend_id: True}
    assert friend_id in {
        str(entry["uid"]) for entry in sessions.fb_friends_str(UID)
    }, "linked player was missing from the in-game hire-friends list"


def test_round_table_rejects_fake_players_and_persists_real_rewards(tmp):
    table_id, x, y = Constant.ID_BUILDING_ROUND_TABLE, 66, 66
    save = sessions.session(UID)
    town = save["maps"][0]
    town["items"].append([
        table_id, x, y, 0, 0, 0, [],
        {"si": [f"accepted-helper-{i}" for i in range(8)], "sif": {}},
    ])
    before_gold, before_xp = town["coins"], town["xp"]
    before_gifts = list(save["privateState"]["gifts"])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ASSIST_SEND_FEED,
         "args": [x, y, 0, table_id, "does-not-exist"]},
        {"cmd": Constant.CMD_FINISH_SI,
         "args": [x, y, 0, table_id, 999999, 999999,
                  Constant.ID_UNIT_XENA]},
    ]))
    reloaded = _reload()
    table = _item(reloaded, table_id, x, y)
    assert table[7]["sif"] == [] and table[7]["si"] == []
    assert reloaded["maps"][0]["coins"] == before_gold + 1000
    assert reloaded["maps"][0]["xp"] == before_xp + 100
    gifts = reloaded["privateState"]["gifts"]
    assert len(gifts) > Constant.ID_UNIT_XENA
    assert gifts[Constant.ID_UNIT_XENA] == (
        (before_gifts[Constant.ID_UNIT_XENA]
         if len(before_gifts) > Constant.ID_UNIT_XENA else 0) + 1
    ), "Round Table accepted client-supplied reward values or lost its unit"


def test_live_enemy_camp_survives_reload_without_respawning(tmp):
    _set_now(_local(12, 10))
    marker = Constant.ID_BUILDING_TREASURE_CHEST
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [marker, 70, 70, 0, 0, 1, 1, 0]},
    ]))
    town = sessions.session(UID)["maps"][0]
    assert town["enemyCampActive"] == 1
    assert town["timestampLastTreasure"] == _local(12, 10)

    command.command(UID, _batch([
        {"cmd": Constant.CMD_END_ATTACK, "args": [json.dumps({
            "win": 0, "resources": {"g": 0, "x": 0},
        })]},
    ]))

    # Returning from PvP/reloading later must restore this exact saved camp,
    # not let MapInitializer generate a replacement at a random position.
    _set_now(_local(12, 12))
    get_player_info.get_player_info(UID)
    assert town["timestampLastTreasure"] == _local(12, 12)
    assert any(item[0] == marker and item[1:3] == [70, 70]
               for item in town["items"])

    _set_now(_local(12, 13))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_COLLECT_TREASURE,
         "args": [100, 20, 1, 0, 0, 0]},
    ]))
    assert town["enemyCampActive"] == 0
    assert town["timestampLastTreasure"] == _local(12, 13), \
        "clearing the camp did not start the real cooldown"


def test_natural_resources_initialize_once_and_do_not_regrow(tmp):
    stone = Constant.ID_BUILDING_STONE_1
    regen = Constant.ID_BUILDING_REGEN_STONE
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [stone, 72, 72, 0, 0, 1, 1, 0]},
    ]))
    town = sessions.session(UID)["maps"][0]
    assert town["naturalResourcesInitialized"] == 1

    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [stone, 74, 74, 0, 0, 1, 1, 0]},
        {"cmd": Constant.CMD_BUY,
         "args": [regen, 72, 72, 0, 0, 1, 1, 0]},
    ]))
    assert not any(item[0] == stone and item[1:3] == [74, 74]
                   for item in town["items"]), "wild stone respawned on reload"
    assert not any(item[0] == regen for item in town["items"]), \
        "depleted stone installed a three-hour regrowth blocker"


def test_destroyed_wall_is_removed_and_stays_removed(tmp):
    """Assault/Heavy Siege destruction opens the occupied wall tile.

    The client clears its pathfinding grid when it removes the BuildingReference;
    this server assertion covers the persistent half of that same lifecycle.
    """
    wall_id, x, y = 49988, 68, 68
    town = sessions.session(UID)["maps"][0]
    town["items"].append([wall_id, x, y, 0, 0, 0, [], {}])

    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, wall_id, 0, 1, "KILL"]},
    ]))
    assert not any(
        item[0] == wall_id and item[1:3] == [x, y]
        for item in town["items"]
    ), "destroyed wall remained on the saved map"
    assert not any(
        item[0] == wall_id and item[1:3] == [x, y]
        for item in _reload()["maps"][0]["items"]
    ), "destroyed wall returned after refresh"


def test_deployed_unit_sale_refund_and_removal_survive_reload(tmp):
    gold_unit_id, gx, gy = 533, 57, 57
    cash_unit_id, cx, cy = 771, 58, 58
    save = sessions.session(UID)
    town = save["maps"][0]
    town["coins"] = 100
    town["items"].extend([
        [gold_unit_id, gx, gy, 0, 0, 0, [], {}],
        [cash_unit_id, cx, cy, 0, 0, 0, [], {}],
    ])

    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [gx, gy, gold_unit_id, 0, 0,
                  Constant.SELL_REASON_BULLDOZE]},
        {"cmd": Constant.CMD_SELL,
         "args": [cx, cy, cash_unit_id, 0, 0,
                  Constant.SELL_REASON_BULLDOZE]},
    ]))

    reloaded = _reload()
    town = reloaded["maps"][0]
    assert town["coins"] == 115, \
        "300-gold unit did not retain its configured 5% (15 gold) resale"
    assert reloaded["playerInfo"]["cash"] == 100, \
        "cash-bought unit incorrectly refunded premium cash"
    assert not any(
        item[0] in (gold_unit_id, cash_unit_id)
        and item[1:3] in ([gx, gy], [cx, cy])
        for item in town["items"]
    ), "sold deployed unit returned after refresh"


def test_collectible_drop_and_collection_shape_survive_reload(tmp):
    # Old saves may have neither field. Player-info must seed all 24 collection
    # slots before a PvP/harvest drop is persisted.
    save = sessions.session(UID)
    save["privateState"].pop("collections", None)
    save["privateState"].pop("collectionsCompleted", None)
    get_player_info.get_player_info(UID)
    assert len(save["privateState"]["collections"]) == 24

    command.command(UID, _batch([
        {"cmd": Constant.CMD_ADD_COLLECTABLE, "args": [5, 3]},
        {"cmd": Constant.CMD_ADD_COLLECTABLE, "args": [5, 3]},
    ]))
    reloaded = _reload()["privateState"]
    assert reloaded["collections"][5][3] == 2, \
        "earned collectible count disappeared or was stored at the wrong slot"
    assert reloaded["collectionsCompleted"] == []


def test_merge_did_not_duplicate_social_staffing_handler(tmp):
    source = open(command.__file__).read()
    marker = "elif cmd == Constant.CMD_BUY_SI_HELP:"
    assert source.count(marker) == 1, \
        "merge introduced duplicate/dead buy_si_help command handlers"


def test_unit_warehouse_store_deploy_and_capacity_persist(tmp):
    warehouse_id, wx, wy = Constant.ID_BUILDING_UNIT_WAREHOUSE, 60, 60
    unit_id, ux, uy = 533, 57, 57
    second_unit_id, sx, sy = 771, 58, 58
    town = sessions.session(UID)["maps"][0]
    town["items"].extend([
        [warehouse_id, wx, wy, 0, 0, 0, [], {}],
        [unit_id, ux, uy, 0, 0, 0, [], {}],
        [second_unit_id, sx, sy, 0, 0, 0, [], {}],
    ])

    # The building's included slot works immediately. Buying for 2 cash adds
    # a second slot; it is not required to activate the first one.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_ADD_UNIT_WAREHOUSE,
         "args": [ux, uy, 0, unit_id]},
        {"cmd": Constant.CMD_BUY_WAREHOUSE_CAPACITY_NEW, "args": [0]},
        {"cmd": Constant.CMD_ADD_UNIT_WAREHOUSE,
         "args": [sx, sy, 0, second_unit_id]},
    ]))
    reloaded = _reload()
    town = reloaded["maps"][0]
    assert reloaded["playerInfo"]["cash"] == 98, \
        "the configured 2-cash warehouse slot price was not persisted"
    assert town["warehouseAditionalCapacitySingle"] == 2
    assert town["warehousedUnits"] == {
        str(unit_id): 1,
        str(second_unit_id): 1,
    }
    assert not any(
        item[0] in (unit_id, second_unit_id)
        and item[1:3] in ([ux, uy], [sx, sy])
        for item in town["items"]
    ), "stored units still counted as deployed/population units"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_PLACE_WAREHOUSED_ITEM,
         "args": [unit_id, 62, 62, 0, 0]},
    ]))
    town = _reload()["maps"][0]
    assert town["warehousedUnits"] == {str(second_unit_id): 1}
    assert any(
        item[0] == unit_id and item[1:3] == [62, 62]
        for item in town["items"]
    ), "warehouse deployment did not survive refresh"


def test_storing_unit_warehouse_moves_units_to_general_storage(tmp):
    warehouse_id, wx, wy = Constant.ID_BUILDING_UNIT_WAREHOUSE, 60, 60
    unit_id = 533
    save = sessions.session(UID)
    town = save["maps"][0]
    town["items"].append([warehouse_id, wx, wy, 0, 0, 0, [], {}])
    town["warehouseAditionalCapacitySingle"] = 3
    town["warehousedUnits"] = {str(unit_id): 2}

    command.command(UID, _batch([
        {"cmd": Constant.CMD_STORE_ITEM,
         "args": [wx, wy, 0, warehouse_id]},
        {"cmd": Constant.CMD_RESET_WAREHOUSE, "args": [0]},
    ]))
    reloaded = _reload()
    town = reloaded["maps"][0]
    gifts = reloaded["privateState"]["gifts"]
    assert town["warehousedUnits"] == {}
    assert town["warehouseAditionalCapacitySingle"] == 3, \
        "moving the Warehouse incorrectly erased purchased capacity"
    assert gifts[unit_id] == 2, \
        "Warehouse contents were lost instead of moved to Gifts/Storage"
    assert gifts[warehouse_id] == 1


TESTS = [
    test_animal_spawn_budget_resets_on_next_local_day,
    test_dragon_nest_progress_survives_second_dragon_reload,
    test_villager_assignment_and_work_timer_survive_reload,
    test_second_worker_yield_and_moving_producer_preserve_progress,
    test_late_worker_bonus_is_prorated_and_client_count_is_ignored,
    test_stone_mine_staffing_and_open_state_survive_reload,
    test_saved_players_are_not_automatic_neighbors,
    test_invalid_market_values_and_xp_level_are_repaired_on_load,
    test_quest_casualties_and_rescued_units_persist,
    test_pvp_history_limits_and_casualties_persist,
    test_market_staffing_trade_and_allies_choice_persist,
    test_hire_friends_staffs_social_buildings_and_persists,
    test_round_table_rejects_fake_players_and_persists_real_rewards,
    test_live_enemy_camp_survives_reload_without_respawning,
    test_natural_resources_initialize_once_and_do_not_regrow,
    test_destroyed_wall_is_removed_and_stays_removed,
    test_deployed_unit_sale_refund_and_removal_survive_reload,
    test_collectible_drop_and_collection_shape_survive_reload,
    test_merge_did_not_duplicate_social_staffing_handler,
    test_unit_warehouse_store_deploy_and_capacity_persist,
    test_storing_unit_warehouse_moves_units_to_general_storage,
]


def main():
    passed = failed = 0
    for test in TESTS:
        tmp = _fresh_env()
        _set_now(_local(12, 10))
        try:
            test(tmp)
            print(f"PASS  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
