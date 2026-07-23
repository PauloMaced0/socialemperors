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


def test_stone_mine_staffing_and_open_state_survive_reload(tmp):
    save = sessions.session(UID)
    mine_id, x, y = 16, 62, 62
    save["maps"][0]["items"].append([mine_id, x, y, 0, 0, 0])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, mine_id]},
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
    test_stone_mine_staffing_and_open_state_survive_reload,
    test_market_staffing_trade_and_allies_choice_persist,
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
