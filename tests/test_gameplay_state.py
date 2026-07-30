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
from get_game_config import get_attribute_from_item_id, get_game_config


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
    assert opened[7]["staffRoles"] == ["Geologist", "Miner"]
    assert opened[7]["staffRoster"] == [0, 0], \
        "completed staff identities were discarded instead of being reusable"


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


def test_pvp_load_strips_defenders_enemy_camp(tmp):
    # Attacking/visiting a human player must not spawn their neutral enemy camp
    # (goblins/trolls, troll towers, prisoners) - those would attack the
    # attacker. The defender's own army/defences stay, and the defender's real
    # save keeps its camp.
    other = "test-gameplay-camp-0003"
    _add_saved_player(tmp, other, "Camp Empire")
    sessions.load_saved_villages()
    dm = sessions.session(other)["maps"][0]
    dm["race"] = "h"
    dm["enemyCampActive"] = 1
    dm["items"].extend([
        [525, 10, 10, 0, 0, 0],   # goblin (troll race)
        [526, 11, 10, 0, 0, 0],   # goblin
        [601, 16, 10, 0, 0, 0],   # Rhinorider - high-tier camp (race t)
        [590, 17, 10, 0, 0, 0],   # Troll Healer - high-tier camp (race t)
        [291, 12, 10, 0, 0, 0],   # troll camp tower (race t building)
        [83, 13, 10, 0, 0, 0],    # prisoner (camp marker, race n)
        [536, 14, 10, 0, 0, 0],   # good troll - player-ownable (race h), must stay
        [29, 15, 10, 0, 0, 0],    # Tower I - defender's real defence, must stay
    ])

    served = get_player_info.get_neighbor_info(other, 0)["map"]
    ids = [it[0] for it in served["items"] if it]
    for gid in (525, 526, 601, 590, 291, 83):
        assert gid not in ids, f"camp entity {gid} served to attacker"
    assert 536 in ids, "player-owned good troll wrongly stripped"
    assert 29 in ids, "defender's real tower wrongly stripped"
    assert served.get("enemyCampActive") == 0, "camp left active in PvP map"

    # Defender's own save keeps the camp for their own game.
    own = sessions.session(other)["maps"][0]
    own_ids = [it[0] for it in own["items"] if it]
    assert 525 in own_ids and 291 in own_ids, \
        "stripping the served copy corrupted the defender's real camp"


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
    attacker_town = sessions.session(UID)["maps"][0]
    attacker_before = sum(1 for item in attacker_town["items"] if item[0] == 512)
    if attacker_before == 0:
        attacker_town["items"].append([512, 59, 59, 0, 0, 0, [], {}])
        attacker_before = 1

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
            "attacker_units": [[512, attacker_before, 1, 0]],
            "victim_units": [[516, 1, 1, 0]],
            "win": 1,
            "voluntary_end": 1,
            "honor": 3,
        })]},
    ]))

    attacker_state = sessions.session(UID)["privateState"]
    defender_state = sessions.session(defender_id)["privateState"]
    assert attacker_state["attacksWon"] == 1
    assert attacker_state["attacksSent"][-1].get("description")
    assert defender_state["attacksLost"] == 1
    assert defender_state["attacksReceived"][-1]["viewPending"] == 1
    assert sum(
        1 for item in sessions.session(UID)["maps"][0]["items"]
        if item[0] == 512
    ) == attacker_before - 1, \
        "withdrawing from PvP restored an attacker casualty"
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


def test_zero_staff_social_building_stays_locked_on_browser_reload(tmp):
    """A live browser reload must perform the same repair as server startup.

    Missing attrs used to be interpreted by the client as an already-open
    building.  Buying one worker happened to create attrs.si, which explains
    why only the completely empty staffing page could be bypassed.
    """
    save = sessions.session(UID)
    market_id, x, y = Constant.ID_BUILDING_MARKET_1, 64, 64
    save["maps"][0]["items"].append([market_id, x, y, 0, 0, 0])

    get_player_info.get_player_info(UID, 0)

    market = _item(save, market_id, x, y)
    assert len(market) >= 8
    assert market[7]["si"] == [], \
        "zero-staff Market became operational after browser reload"
    persisted = json.load(open(os.path.join(
        tmp, f"{UID}.save.json"
    )))
    persisted_market = _item(persisted, market_id, x, y)
    assert persisted_market[7]["si"] == [], \
        "browser-load staffing repair was not persisted"


def test_reload_bootstrap_cannot_create_a_phantom_social_worker(tmp):
    save = sessions.session(UID)
    market_id, x, y = Constant.ID_BUILDING_MARKET_1, 64, 64
    save["maps"][0]["items"].append([
        market_id, x, y, 0, 0, 0, [], {"si": []},
    ])
    cash_before = save["playerInfo"]["cash"]

    # countAllBuildings() used this fifth argument during map load. It was
    # never a click on "fill with 2 cash" and must not mutate either slot or
    # currency.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP,
         "args": [x, y, 0, market_id, 1]},
    ]))
    market = _item(_reload(), market_id, x, y)
    assert market[7]["si"] == [], \
        "browser reload silently filled the first Market role"
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_before, \
        "hidden reload staffing command charged cash"


def test_scripted_zeppelin_unlock_does_not_charge_worker_cash(tmp):
    save = sessions.session(UID)
    tower_id, x, y = Constant.ID_BUILDING_ZEPPELIN_TOWER, 59, 59
    save["maps"][0]["items"].append([
        tower_id, x, y, 0, 0, 0, [], {"si": []},
    ])
    cash_before = save["playerInfo"]["cash"]

    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP,
         "args": [x, y, 0, tower_id, 1]},
    ]))
    tower = _item(_reload(), tower_id, x, y)
    assert tower[7]["si"] == [0], \
        "scripted Zeppelin progression worker was rejected"
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_before, \
        "automatic Zeppelin progression silently charged worker cash"


def test_legacy_auto_opened_harbour_returns_to_manual_staffing(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    harbour_id, x, y = Constant.ID_BUILDING_DOCK, 65, 42
    town.pop("harbourManualStaffingVersion", None)
    town["items"].append([
        harbour_id, x, y, 0, 0, 0, [],
        {
            "si": None,
            "staffRoles": ["Captain", "Cabin boy", "Helmsman"],
            "staffRoster": [0, 0, 0],
        },
    ])
    cash_before = save["playerInfo"]["cash"]

    get_player_info.get_player_info(UID, 0)
    harbour = _item(save, harbour_id, x, y)
    assert harbour[7]["si"] == [], \
        "the old Dock-operative reload state remained fully staffed"
    assert harbour[7]["staffRoles"] == []
    assert harbour[7]["staffRoster"] == []

    # A cached pre-fix client may still submit the old hidden fifth argument.
    # The server must reject it instead of silently reopening the Harbor.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP,
         "args": [x, y, 0, harbour_id, 1]},
    ]))
    harbour = _item(_reload(), harbour_id, x, y)
    assert harbour[7]["si"] == []
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_before

    # A real click has four arguments and fills exactly one paid role.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY_SI_HELP,
         "args": [x, y, 0, harbour_id]},
    ]))
    harbour = _item(_reload(), harbour_id, x, y)
    assert harbour[7]["si"] == [0]
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_before - 3


def test_unstaffed_social_building_cannot_upgrade_past_roles(tmp):
    save = sessions.session(UID)
    market_id, upgraded_id = Constant.ID_BUILDING_MARKET_1, 188
    x, y = 64, 64
    town = save["maps"][0]
    town["wood"] = 100000
    town["coins"] = 100000
    town["items"].append([
        market_id, x, y, 0, 0, 0, [], {"si": []},
    ])

    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, market_id, 0, 0,
                  Constant.SELL_REASON_UPGRADE]},
        {"cmd": Constant.CMD_BUY,
         "args": [upgraded_id, x, y, 0, 0, 0, 1, 0]},
    ]))
    reloaded = _reload()
    assert any(item[0] == market_id and item[1:3] == [x, y]
               for item in reloaded["maps"][0]["items"])
    assert not any(item[0] == upgraded_id and item[1:3] == [x, y]
                   for item in reloaded["maps"][0]["items"]), \
        "upgrade replacement bypassed the Market's three staff roles"

    # A genuinely opened Market is still allowed to upgrade.
    market = _item(reloaded, market_id, x, y)
    market[7]["si"] = None
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, market_id, 0, 0,
                  Constant.SELL_REASON_UPGRADE]},
        {"cmd": Constant.CMD_BUY,
         "args": [upgraded_id, x, y, 0, 0, 0, 1, 0]},
    ]))
    assert any(item[0] == upgraded_id and item[1:3] == [x, y]
               for item in _reload()["maps"][0]["items"]), \
        "fully staffed Market could not perform a legitimate upgrade"


def test_staff_carries_by_role_and_only_new_upgrade_jobs_are_vacant(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    town["stone"] = town["wood"] = town["coins"] = 100000
    x, y = 61, 61
    town["items"].append([
        16, x, y, 0, 0, 0, [],
        {
            "si": None,
            "staffRoles": ["Geologist", "Miner"],
            "staffRoster": ["friend-geologist", "friend-miner"],
        },
    ])

    # Stone I -> II has no staffing popup. Its roster remains attached so
    # the later social tier can inherit the matching jobs.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, 16, 0, 0, Constant.SELL_REASON_UPGRADE]},
        {"cmd": Constant.CMD_BUY,
         "args": [17, x, y, 0, 0, 0, 1, "b"]},
    ]))
    level_two = _item(_reload(), 17, x, y)
    assert level_two[7]["staffRoles"] == ["Geologist", "Miner"]
    assert level_two[7]["staffRoster"] == [
        "friend-geologist", "friend-miner",
    ]

    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, 17, 0, 0, Constant.SELL_REASON_UPGRADE]},
        {"cmd": Constant.CMD_BUY,
         "args": [18, x, y, 0, 0, 0, 1, "b"]},
    ]))
    level_three = _item(_reload(), 18, x, y)
    assert level_three[7]["si"] == [
        "friend-geologist", "friend-miner",
    ], "Stone Mine III did not preserve its two matching level-I workers"
    assert level_three[7]["staffRoles"] == ["Geologist", "Miner"]

    for _ in range(3):
        command.command(UID, _batch([
            {"cmd": Constant.CMD_BUY_SI_HELP, "args": [x, y, 0, 18]},
        ]))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_FINISH_SI, "args": [x, y, 0, 18]},
    ]))
    opened = _item(_reload(), 18, x, y)
    assert opened[7]["si"] is None
    assert opened[7]["staffRoles"] == [
        "Geologist", "Miner", "Cartographer", "Engineer", "Supervisor",
    ]
    assert opened[7]["staffRoster"][:2] == [
        "friend-geologist", "friend-miner",
    ]


def test_same_role_workshop_upgrade_does_not_rehire_staff(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    town["stone"] = town["wood"] = town["coins"] = 100000
    x, y = 58, 58
    roles = [
        "Engineer", "Assistant", "Apprentice", "Bullet Crafter",
        "Powder Mixer", "Transporter", "Carpenter", "Alchemist",
    ]
    town["items"].append([
        49, x, y, 0, 0, 0, [],
        {"si": None, "staffRoles": roles, "staffRoster": [0] * len(roles)},
    ])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [x, y, 49, 0, 0, Constant.SELL_REASON_UPGRADE]},
        {"cmd": Constant.CMD_BUY,
         "args": [50, x, y, 0, 0, 0, 1, "b"]},
    ]))
    upgraded = _item(_reload(), 50, x, y)
    assert upgraded[7]["si"] is None, \
        "Workshop III charged for Workshop II's identical eight jobs again"
    assert upgraded[7]["staffRoles"] == roles


def test_unstaffed_cathedral_cannot_train_monks(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    cathedral_id, monk_id = Constant.ID_BUILDING_CATHEDRAL, 569
    x, y = 60, 60
    spawn_x, spawn_y = 62, 60
    town["coins"] = town["food"] = 100000
    save["playerInfo"]["cash"] = 100
    town["items"].append([
        cathedral_id, x, y, 0, 0, 0, [], {"si": []},
    ])
    before = {
        "coins": town["coins"],
        "food": town["food"],
        "cash": save["playerInfo"]["cash"],
    }

    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [
             monk_id, spawn_x, spawn_y, 0, 0, 0, 1, "u",
             x, y, cathedral_id,
         ]},
        {"cmd": Constant.CMD_BUY_UNIT_WITH_CASH,
         "args": [
             monk_id, spawn_x, spawn_y, 0, 0, x, y, cathedral_id,
         ]},
    ]))
    reloaded = _reload()
    town = reloaded["maps"][0]
    assert not any(
        item[0] == monk_id and item[1:3] == [spawn_x, spawn_y]
        for item in town["items"]
    ), "Cathedral trained a monk before all twelve staff roles were filled"
    assert town["coins"] == before["coins"]
    assert town["food"] == before["food"]
    assert reloaded["playerInfo"]["cash"] == before["cash"]

    cathedral = _item(reloaded, cathedral_id, x, y)
    cathedral[7]["si"] = [0] * 12
    command.command(UID, _batch([
        {"cmd": Constant.CMD_FINISH_SI,
         "args": [x, y, 0, cathedral_id]},
        {"cmd": Constant.CMD_BUY,
         "args": [
             monk_id, spawn_x, spawn_y, 0, 0, 0, 1, "u",
             x, y, cathedral_id,
         ]},
    ]))
    assert any(
        item[0] == monk_id and item[1:3] == [spawn_x, spawn_y]
        for item in _reload()["maps"][0]["items"]
    ), "a fully staffed Cathedral could not train its monk"


def test_unstaffed_producer_rejects_worker_and_activation(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    mine_id, villager_id = 16, 500
    x, y = 62, 62
    ux, uy = 60, 62
    town["items"].extend([
        [mine_id, x, y, 0, 0, 0, [], {"si": []}],
        [villager_id, ux, uy, 0, 0, 0],
    ])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_PUSH_UNIT,
         "args": [ux, uy, villager_id, x, y, 0]},
        {"cmd": Constant.CMD_ACTIVATE,
         "args": [x, y, 0, mine_id, 2]},
    ]))
    reloaded = _reload()
    mine = _item(reloaded, mine_id, x, y)
    assert mine[6] == [], "unstaffed Stone Mine accepted a worker"
    assert "cp" not in mine[7], "unstaffed Stone Mine started production"
    assert any(
        item[0] == villager_id and item[1:3] == [ux, uy]
        for item in reloaded["maps"][0]["items"]
    ), "rejected worker assignment deleted the villager"


def test_social_staff_requires_target_acceptance_and_persists(tmp):
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
    assert recruitment[7]["si"] == [], "requester filled a role without acceptance"
    assert table[7]["si"] == [], "Round Table request auto-accepted"
    assert table[7]["sif"] == {friend_id: True}
    pending = command.incoming_social_staff_requests(friend_id)
    assert len(pending) == 2, f"staffing requests were not queued: {pending}"
    assert all(command.resolve_social_staff_request(
        friend_id, request["key"], True
    ) for request in pending)

    reloaded = _reload()
    recruitment = _item(reloaded, recruitment_id, rx, ry)
    table = _item(reloaded, table_id, tx, ty)
    assert recruitment[7]["si"] == [friend_id]
    assert table[7]["si"] == [friend_id]
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


def test_natural_resources_use_persisted_random_respawns(tmp):
    stone = Constant.ID_BUILDING_STONE_1
    gold = Constant.ID_BUILDING_GOLD_1
    tree = Constant.ID_BUILDING_TREE_1
    stone_ids = {
        Constant.ID_BUILDING_STONE_1,
        Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3,
        Constant.ID_BUILDING_STONE_4,
    }
    gold_ids = {
        Constant.ID_BUILDING_GOLD_1,
        Constant.ID_BUILDING_GOLD_2,
        Constant.ID_BUILDING_GOLD_3,
        Constant.ID_BUILDING_GOLD_4,
    }
    _set_now(_local(12, 10))
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [stone, 72, 72, 0, 0, 1, 1, 0]},
        {"cmd": Constant.CMD_BUY,
         "args": [gold, 73, 72, 0, 0, 1, 1, 0]},
        {"cmd": Constant.CMD_BUY,
         "args": [tree, 74, 74, 0, 0, 1, 1, 0]},
    ]))
    town = sessions.session(UID)["maps"][0]
    assert town["naturalResourcesInitialized"] == 1

    # A mature map reload may try to run MapInitializer's free population
    # buys again. The server rejects that batch instead of instantly replacing
    # harvested nodes.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [stone, 76, 76, 0, 0, 1, 1, 0]},
    ]))
    assert not any(item[0] == stone and item[1:3] == [76, 76]
                   for item in town["items"]), \
        "browser reload bypassed the natural-resource cooldown"

    # A cached older client may still request same-tile IDs 80/81. The server
    # rejects those placeholders because each harvest already created exactly
    # one persisted random-respawn timer.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [72, 72, stone, 0, 0, Constant.SELL_REASON_HARVEST]},
        {"cmd": Constant.CMD_BUY,
         "args": [
             Constant.ID_BUILDING_REGEN_STONE,
             72, 72, 0, 0, 0, 1, 0,
         ]},
        {"cmd": Constant.CMD_SELL,
         "args": [73, 72, gold, 0, 0, Constant.SELL_REASON_HARVEST]},
        {"cmd": Constant.CMD_BUY,
         "args": [
             Constant.ID_BUILDING_REGEN_GOLD,
             73, 72, 0, 0, 0, 1, 0,
         ]},
    ]))
    assert not any(
        item[0] in (
            Constant.ID_BUILDING_REGEN_GOLD,
            Constant.ID_BUILDING_REGEN_STONE,
        )
        for item in town["items"]
    ), "same-tile mineral placeholder was accepted"
    assert {
        entry["family"] for entry in town["pendingMineralRespawns"]
    } == {"gold", "stone"}

    reloaded = _reload()
    assert len(reloaded["maps"][0]["pendingMineralRespawns"]) == 2, \
        "browser/server reload lost a mineral cooldown"
    town = reloaded["maps"][0]

    # Trees and minerals share the same three-hour persistence rule.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [74, 74, tree, 0, 0, Constant.SELL_REASON_HARVEST]},
    ]))
    assert not any(item[0] == tree and item[1:3] == [74, 74]
                   for item in town["items"])
    _set_now(_local(12, 12))
    get_player_info.get_player_info(UID)
    assert not any(item[0] == tree and item[1:3] == [74, 74]
                   for item in town["items"]), "tree respawned before 3h"
    assert not any(item[0] in stone_ids | gold_ids
                   for item in town["items"]), \
        "gold/stone respawned before their three-hour timer"

    _set_now(_local(12, 13))
    get_player_info.get_player_info(UID)
    assert any(item[0] == tree and item[1:3] == [74, 74]
               for item in town["items"]), "tree did not regrow after 3h"
    respawned_stone = next(
        item for item in town["items"] if item[0] in stone_ids
    )
    respawned_gold = next(
        item for item in town["items"] if item[0] in gold_ids
    )
    # Minerals now regrow on their ORIGINAL harvested tile (like trees), so a
    # reload never relocates a deposit.
    assert respawned_stone[1:3] == [72, 72], \
        "stone did not regrow on its original tile"
    assert respawned_gold[1:3] == [73, 72], \
        "gold did not regrow on its original tile"
    assert town["pendingMineralRespawns"] == []

    reloaded = _reload()
    assert sum(
        1 for item in reloaded["maps"][0]["items"]
        if item[0] == tree and item[1:3] == [74, 74]
    ) == 1, "tree cooldown duplicated the respawn after reload"
    assert sum(
        1 for item in reloaded["maps"][0]["items"]
        if item[0] in stone_ids
    ) == 1, "stone cooldown duplicated its random respawn after reload"
    assert sum(
        1 for item in reloaded["maps"][0]["items"]
        if item[0] in gold_ids
    ) == 1, "gold cooldown duplicated its random respawn after reload"


def test_legacy_mineral_placeholders_migrate_without_resetting_timer(tmp):
    town = sessions.session(UID)["maps"][0]
    town["naturalResourcesInitialized"] = 1
    town["naturalResourceRecoveryVersion"] = 1
    town["items"].append([
        Constant.ID_BUILDING_REGEN_STONE,
        70, 71, 0, _local(12, 10), 0,
    ])

    _set_now(_local(12, 12))
    get_player_info.get_player_info(UID)
    assert not any(
        item[0] == Constant.ID_BUILDING_REGEN_STONE
        for item in town["items"]
    ), "legacy same-tile placeholder survived migration"
    assert town["pendingMineralRespawns"] == [{
        "family": "stone",
        "source_x": 70,
        "source_y": 71,
        "at": _local(12, 13),
    }]
    assert not any(
        item[0] in {
            Constant.ID_BUILDING_STONE_1,
            Constant.ID_BUILDING_STONE_2,
            Constant.ID_BUILDING_STONE_3,
            Constant.ID_BUILDING_STONE_4,
        }
        for item in town["items"]
    ), "legacy cooldown was completed early during migration"

    _set_now(_local(12, 13))
    get_player_info.get_player_info(UID)
    replacement = next(
        item for item in town["items"]
        if item[0] in {
            Constant.ID_BUILDING_STONE_1,
            Constant.ID_BUILDING_STONE_2,
            Constant.ID_BUILDING_STONE_3,
            Constant.ID_BUILDING_STONE_4,
        }
    )
    # Regrowth restores the deposit on its original tile now, not a random one.
    assert replacement[1:3] == [70, 71]
    assert town["pendingMineralRespawns"] == []


def test_mineral_regrows_in_place_from_regen_placeholder(tmp):
    # Minerals actually deplete by the client placing a same-tile regen
    # placeholder (80/81), not a harvest sell. The server used to reject it, so
    # gold/stone never regrew. Now it queues an in-place regrow at that exact
    # tile, so the deposit grows back inside the player's territory.
    _set_now(_local(12, 10))
    town = sessions.session(UID)["maps"][0]
    stone_ids = {
        Constant.ID_BUILDING_STONE_1, Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3, Constant.ID_BUILDING_STONE_4,
    }
    town["items"] = [it for it in town["items"] if not it or int(it[0]) not in stone_ids]
    town["items"].append([Constant.ID_BUILDING_STONE_1, 44, 44, 0, _local(12, 10), 0])
    town["pendingMineralRespawns"] = []

    # Client mines it out: removes the deposit, then places the regen object.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [44, 44, Constant.ID_BUILDING_STONE_1, 0, 0, Constant.SELL_REASON_HARVEST]},
        {"cmd": Constant.CMD_BUY,
         "args": [Constant.ID_BUILDING_REGEN_STONE, 44, 44, 1, 0, 0, 1, "b"]},
    ]))
    pend = town["pendingMineralRespawns"]
    assert len(pend) == 1 and pend[0]["family"] == "stone", \
        f"depletion did not queue exactly one in-place regrow: {pend}"
    assert (pend[0]["source_x"], pend[0]["source_y"]) == (44, 44), \
        "regrow queued at the wrong tile (not in place)"
    # The regen placeholder itself must not persist as a map item.
    assert not any(it and int(it[0]) == Constant.ID_BUILDING_REGEN_STONE
                   for it in town["items"]), "regen placeholder leaked into items"

    _set_now(_local(12, 10) + 3 * 60 * 60 + 60)
    get_player_info.get_player_info(UID)
    assert any(it and int(it[0]) in stone_ids and it[1] == 44 and it[2] == 44
               for it in town["items"]), "stone did not regrow on its original tile"


def test_mineral_respawn_works_above_legacy_21_cap(tmp):
    # The stock village seeds ~24 gold/stone nodes per family - above the old
    # hardcoded cap of 21, which discarded every respawn timer once the family
    # sat at/over 21, so harvested gold/stone never came back. A pending timer
    # maps 1:1 to a real harvest, so restoring it can't exceed the original
    # population; it must succeed regardless of the count.
    _set_now(_local(12, 10))
    town = sessions.session(UID)["maps"][0]
    stone_ids = (
        Constant.ID_BUILDING_STONE_1,
        Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3,
        Constant.ID_BUILDING_STONE_4,
    )
    town["items"] = [
        item for item in town["items"]
        if not item or int(item[0]) not in stone_ids
    ]
    for index in range(24):  # seed a family well above the old cap of 21
        town["items"].append(
            [stone_ids[index % len(stone_ids)], index, 80, 0, _local(12, 10), 0]
        )
    n0 = sum(1 for item in town["items"] if item[0] in stone_ids)
    assert n0 == 24

    # Harvest one deposit -> 23 present + one pending respawn.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [5, 80, stone_ids[1], 0, 0, Constant.SELL_REASON_HARVEST]},
    ]))
    assert sum(1 for item in town["items"] if item[0] in stone_ids) == 23
    assert len(town["pendingMineralRespawns"]) == 1

    # Before 3h: still 23. After 3h: restored to 24 (not discarded by a cap).
    _set_now(_local(12, 12))
    get_player_info.get_player_info(UID)
    assert sum(1 for item in town["items"] if item[0] in stone_ids) == 23, \
        "mineral respawned before its 3h timer"

    _set_now(_local(12, 13))
    get_player_info.get_player_info(UID)
    assert sum(1 for item in town["items"] if item[0] in stone_ids) == 24, \
        "gold/stone did not respawn because the family was above 21"
    assert town["pendingMineralRespawns"] == []
    assert any(item[0] in stone_ids and item[1:3] == [5, 80]
               for item in town["items"]), "respawn not on original tile"


def test_legacy_empty_map_gets_one_stock_resource_repopulation(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    natural_ids = {
        Constant.ID_BUILDING_TREE_1,
        Constant.ID_BUILDING_TREE_2,
        Constant.ID_BUILDING_TREE_3,
        Constant.ID_BUILDING_STONE_1,
        Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3,
        Constant.ID_BUILDING_STONE_4,
        Constant.ID_BUILDING_GOLD_1,
        Constant.ID_BUILDING_GOLD_2,
        Constant.ID_BUILDING_GOLD_3,
        Constant.ID_BUILDING_GOLD_4,
        Constant.ID_BUILDING_REGEN_GOLD,
        Constant.ID_BUILDING_REGEN_STONE,
    }
    town["items"] = [
        item for item in town["items"]
        if not item or int(item[0]) not in natural_ids
    ]
    town["pendingTreeRespawns"] = []
    town["naturalResourcesInitialized"] = 1
    town.pop("naturalResourceRecoveryVersion", None)

    response = get_player_info.get_player_info(UID, 0)
    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    assert town["naturalResourcesInitialized"] == 0
    assert marker not in response["privateState"]["arrayAnimals"], \
        "legacy empty map was not reopened for stock resource population"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [Constant.ID_BUILDING_GOLD_1, 71, 71, 0, 0, 1, 1, 0]},
        {"cmd": Constant.CMD_BUY,
         "args": [Constant.ID_BUILDING_STONE_1, 72, 72, 0, 0, 1, 1, 0]},
        {"cmd": Constant.CMD_BUY,
         "args": [Constant.ID_BUILDING_TREE_1, 73, 73, 0, 0, 1, 1, 0]},
    ]))
    reloaded = _reload()
    town = reloaded["maps"][0]
    assert town["naturalResourcesInitialized"] == 1
    assert all(any(item[0] == item_id for item in town["items"])
               for item_id in (
                   Constant.ID_BUILDING_GOLD_1,
                   Constant.ID_BUILDING_STONE_1,
                   Constant.ID_BUILDING_TREE_1,
               )), "one-time recovery did not persist the new resources"

    # Once recovered, a later reload cannot invoke population as an instant
    # respawn shortcut.
    get_player_info.get_player_info(UID, 0)
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [Constant.ID_BUILDING_GOLD_1, 74, 74, 0, 0, 1, 1, 0]},
    ]))
    assert not any(item[0] == Constant.ID_BUILDING_GOLD_1
                   and item[1:3] == [74, 74]
                   for item in sessions.session(UID)["maps"][0]["items"]), \
        "legacy recovery remained enabled after its one allowed population"


def test_reload_marker_relocks_so_trees_do_not_wander(tmp):
    # A town left with naturalResourcesInitialized=0 while it still HAS wild
    # resources would clear the client reload marker on every load, so the
    # client repopulation pass re-randomizes ("wanders") existing trees each
    # reload. Serving must re-lock the flag and set the marker.
    save = sessions.session(UID)
    save["playerInfo"]["completed_tutorial"] = 1
    town = save["maps"][0]
    town["items"].append([Constant.ID_BUILDING_TREE_1, 55, 55, 0, 0, 0])
    town["naturalResourcesInitialized"] = 0

    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    body = get_player_info.get_player_info(UID, 0)
    assert town["naturalResourcesInitialized"] == 1, \
        "reload marker flag not re-locked while resources are present"
    assert body["privateState"]["arrayAnimals"].get(marker) == 1, \
        "client repopulation not suppressed -> trees would wander on reload"


def test_ship_quest_requires_a_fully_staffed_harbour(tmp):
    town = sessions.session(UID)["maps"][0]
    quest_id = str(get_game_config()["globals"]["ISLE_ORDER"][0])
    harbour = [
        Constant.ID_BUILDING_DOCK,
        68, 68, 0, _local(12, 10), 0, [], {"si": []},
    ]
    town["items"].append(harbour)

    assert command.do_command(
        UID, Constant.CMD_START_QUEST, [quest_id, 0]
    ) is False, "Ship Land opened while the Harbor was unstaffed"

    harbour[7]["si"] = None
    assert command.do_command(
        UID, Constant.CMD_START_QUEST, [quest_id, 0]
    ) is not False, "a completed Harbor did not unlock Ship Land"


def test_producer_limit_applies_across_upgrade_family(tmp):
    town = sessions.session(UID)["maps"][0]
    town["coins"] = 100000
    # Owning Gold Mine II must prevent placing Gold Mine I beside it.
    town["items"].append([14, 70, 70, 0, 0, 0, [], {"si": None}])
    command.command(UID, _batch([
        {"cmd": Constant.CMD_BUY,
         "args": [13, 72, 72, 0, 0, 0, 1, 0]},
    ]))
    assert not any(item[0] == 13 and item[1:3] == [72, 72]
                   for item in town["items"]), \
        "a lower mine tier bypassed the one-per-family limit"

    # A real upgrade removes the old tier before placing the new one.
    command.command(UID, _batch([
        {"cmd": Constant.CMD_SELL,
         "args": [70, 70, 14, 0, 0, "UPGR"]},
        {"cmd": Constant.CMD_BUY,
         "args": [15, 70, 70, 0, 0, 0, 1, 0]},
    ]))
    assert any(item[0] == 15 and item[1:3] == [70, 70]
               for item in _reload()["maps"][0]["items"]), \
        "the family limit blocked a legitimate upgrade"


def test_weather_spell_and_mana_state_survive_reload(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    town["level"] = 50
    town["coins"] = 100000
    save["playerInfo"]["cash"] = 100
    state = save["privateState"]
    state["mana"] = 0
    state["magics"] = {}
    state["unlockedSkins"] = {}

    command.command(UID, _batch([
        {"cmd": Constant.CMD_UNLOCK_SKIN, "args": ["2"]},
        {"cmd": Constant.CMD_SET_SKIN, "args": [0, "2"]},
        {"cmd": Constant.CMD_BUY_MAGIC, "args": [2, 0, 0]},
        {"cmd": Constant.CMD_USE_MAGIC, "args": [2]},
        {"cmd": Constant.CMD_BUY_MANA, "args": [0, 0]},
    ]))
    reloaded = _reload()
    state = reloaded["privateState"]
    assert reloaded["maps"][0]["skin"] == 2
    assert state["unlockedSkins"] == {"2": "true"}
    assert state["magics"]["2"] == 1, "spell use count disappeared"
    # Learning Fire Havoc grants its five mana, casting spends five, and the
    # explicit purchase adds five.
    assert state["mana"] == 5
    cash_after = reloaded["playerInfo"]["cash"]
    coins_after = reloaded["maps"][0]["coins"]

    command.command(UID, _batch([
        {"cmd": Constant.CMD_UNLOCK_SKIN, "args": ["2"]},
        {"cmd": Constant.CMD_BUY_MAGIC, "args": [2, 0, 0]},
    ]))
    assert sessions.session(UID)["playerInfo"]["cash"] == cash_after, \
        "reselecting an unlocked weather theme charged cash again"
    assert sessions.session(UID)["maps"][0]["coins"] == coins_after, \
        "buying an already learned spell charged gold again"


def test_building_damage_and_repair_survive_reload(tmp):
    item_id, x, y = 1, 74, 74  # House I
    town = sessions.session(UID)["maps"][0]
    town["items"].append([item_id, x, y, 0, 0, 0, [], {}])
    maximum = int(get_attribute_from_item_id(item_id, "life"))
    damaged = maximum // 2
    command.command(UID, _batch([
        {"cmd": "set_item_health", "args": [x, y, 0, item_id, damaged]},
    ]))
    item = _item(_reload(), item_id, x, y)
    assert item[7]["hp"] == damaged, "building healed on browser/server reload"

    command.command(UID, _batch([
        {"cmd": "set_item_health", "args": [x, y, 0, item_id, maximum]},
    ]))
    assert "hp" not in _item(_reload(), item_id, x, y)[7], \
        "fully repaired building remained marked as damaged"


def test_home_unit_damage_and_healing_survive_reload(tmp):
    item_id, x, y = 512, 76, 76  # Light Knight
    town = sessions.session(UID)["maps"][0]
    town["items"].append([item_id, x, y, 0, 0, 0, [], {}])
    maximum = int(get_attribute_from_item_id(item_id, "life"))
    damaged = maximum // 2
    command.command(UID, _batch([
        {"cmd": "set_item_health", "args": [x, y, 0, item_id, damaged]},
    ]))
    assert _item(_reload(), item_id, x, y)[7]["hp"] == damaged, \
        "wounded home unit healed on browser/server reload"

    command.command(UID, _batch([
        {"cmd": "set_item_health", "args": [x, y, 0, item_id, maximum]},
    ]))
    assert "hp" not in _item(_reload(), item_id, x, y)[7], \
        "fully healed home unit remained marked as damaged"


def test_stored_building_uses_owned_storage_not_gifts(tmp):
    item_id, x, y = 1, 78, 78  # House I
    save = sessions.session(UID)
    town = save["maps"][0]
    town["store"] = {}
    save["privateState"]["gifts"] = []
    town["items"].append([item_id, x, y, 0, 0, 0, [], {}])

    command.command(UID, _batch([
        {"cmd": Constant.CMD_STORE_ITEM, "args": [x, y, 0, item_id]},
    ]))
    reloaded = _reload()
    assert reloaded["maps"][0]["store"] == {str(item_id): 1}
    assert reloaded["privateState"]["gifts"] == [], \
        "owned building became a gift and would re-award placement XP"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_PLACE_STORED_ITEM,
         "args": [item_id, x + 1, y, 0, 0]},
    ]))
    reloaded = _reload()
    assert reloaded["maps"][0]["store"] == {}
    assert _item(reloaded, item_id, x + 1, y) is not None


def test_quest_rewards_pay_once_and_progress_by_order_index(tmp):
    save = sessions.session(UID)
    town = save["maps"][0]
    state = save["privateState"]
    town["coins"] = town["xp"] = 0
    state["unlockedQuestIndex"] = 0
    state["questsRank"] = {}
    state["gifts"] = []

    def finish(qid, win=1, difficulty=1):
        command.command(UID, _batch([{
            "cmd": Constant.CMD_END_QUEST,
            "args": [json.dumps({
                "map": 0,
                "resources": {"g": 100, "x": 10},
                "units": [],
                "win": win,
                "duration": 60,
                "voluntary_end": 0,
                "quest_id": qid,
                "item_rewards": {"537": 1},
                "difficulty": difficulty,
            })],
        }]))

    finish("100000006", difficulty=1)
    reloaded = _reload()
    assert reloaded["privateState"]["unlockedQuestIndex"] == 1, \
        "quest id was stored as the unlocked quest index"
    assert reloaded["maps"][0]["coins"] == 100
    assert reloaded["maps"][0]["xp"] == 10
    assert reloaded["privateState"]["gifts"][537] == 1

    # A NEW difficulty pays its own (progressive) prize on first clear.
    finish("100000006", difficulty=2)
    harder = _reload()
    assert harder["maps"][0]["coins"] == 200, \
        "a new difficulty did not pay its own prize"
    assert harder["maps"][0]["xp"] == 20
    assert harder["privateState"]["gifts"][537] == 2
    assert harder["privateState"]["questsRank"]["100000006"] == 2, \
        "the improved star rank was not retained"

    # Replaying an ALREADY-cleared difficulty pays nothing.
    finish("100000006", difficulty=2)
    replayed = _reload()
    assert replayed["maps"][0]["coins"] == 200, \
        "replaying a cleared difficulty paid its prize again"
    assert replayed["maps"][0]["xp"] == 20
    assert replayed["privateState"]["gifts"][537] == 2, \
        "replaying a cleared difficulty paid its unit reward again"

    finish("100000007", win=0)
    assert sessions.session(UID)["privateState"]["unlockedQuestIndex"] == 1
    finish("100000007")
    assert _reload()["privateState"]["unlockedQuestIndex"] == 2


def test_invalid_quest_id_progress_is_repaired_on_load(tmp):
    state = sessions.session(UID)["privateState"]
    state["questsRank"] = {"100000006": 1}
    state["unlockedQuestIndex"] = 100000007
    sessions.save_session(UID)
    assert _reload()["privateState"]["unlockedQuestIndex"] == 1, \
        "legacy quest-id progress still unlocked the complete campaign"


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
    # Index 0 is a 1-based-alignment dummy and MUST be empty, or the client's
    # collection-book loader throws at i=0 (arCollected[0] doesn't exist) and
    # the try/catch drops every earned collectible on reload.
    assert save["privateState"]["collections"][0] == [], \
        "collections[0] must be empty so the client book loader survives reload"

    command.command(UID, _batch([
        {"cmd": Constant.CMD_ADD_COLLECTABLE, "args": [5, 3]},
        {"cmd": Constant.CMD_ADD_COLLECTABLE, "args": [5, 3]},
    ]))
    reloaded = _reload()["privateState"]
    assert reloaded["collections"][5][3] == 2, \
        "earned collectible count disappeared or was stored at the wrong slot"
    assert reloaded["collectionsCompleted"] == []

    # The count must survive a subsequent player-info load (the reload path the
    # player actually hits), and index 0 must stay empty.
    get_player_info.get_player_info(UID)
    after = sessions.session(UID)["privateState"]
    assert after["collections"][5][3] == 2, "collectible lost on player-info reload"
    assert after["collections"][0] == [], "collections[0] repopulated to non-empty"


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
    assert town["warehousedUnits"] == {}
    assert town["warehouseAditionalCapacitySingle"] == 3, \
        "moving the Warehouse incorrectly erased purchased capacity"
    assert town["store"][str(unit_id)] == 2, \
        "Warehouse contents were lost instead of moved to Gifts/Storage"
    assert town["store"][str(warehouse_id)] == 1


TESTS = [
    test_animal_spawn_budget_resets_on_next_local_day,
    test_dragon_nest_progress_survives_second_dragon_reload,
    test_villager_assignment_and_work_timer_survive_reload,
    test_second_worker_yield_and_moving_producer_preserve_progress,
    test_late_worker_bonus_is_prorated_and_client_count_is_ignored,
    test_stone_mine_staffing_and_open_state_survive_reload,
    test_saved_players_are_not_automatic_neighbors,
    test_pvp_load_strips_defenders_enemy_camp,
    test_invalid_market_values_and_xp_level_are_repaired_on_load,
    test_quest_casualties_and_rescued_units_persist,
    test_pvp_history_limits_and_casualties_persist,
    test_market_staffing_trade_and_allies_choice_persist,
    test_zero_staff_social_building_stays_locked_on_browser_reload,
    test_reload_bootstrap_cannot_create_a_phantom_social_worker,
    test_scripted_zeppelin_unlock_does_not_charge_worker_cash,
    test_legacy_auto_opened_harbour_returns_to_manual_staffing,
    test_unstaffed_social_building_cannot_upgrade_past_roles,
    test_staff_carries_by_role_and_only_new_upgrade_jobs_are_vacant,
    test_same_role_workshop_upgrade_does_not_rehire_staff,
    test_unstaffed_cathedral_cannot_train_monks,
    test_unstaffed_producer_rejects_worker_and_activation,
    test_social_staff_requires_target_acceptance_and_persists,
    test_round_table_rejects_fake_players_and_persists_real_rewards,
    test_live_enemy_camp_survives_reload_without_respawning,
    test_natural_resources_use_persisted_random_respawns,
    test_legacy_mineral_placeholders_migrate_without_resetting_timer,
    test_mineral_regrows_in_place_from_regen_placeholder,
    test_mineral_respawn_works_above_legacy_21_cap,
    test_legacy_empty_map_gets_one_stock_resource_repopulation,
    test_reload_marker_relocks_so_trees_do_not_wander,
    test_ship_quest_requires_a_fully_staffed_harbour,
    test_producer_limit_applies_across_upgrade_family,
    test_weather_spell_and_mana_state_survive_reload,
    test_building_damage_and_repair_survive_reload,
    test_home_unit_damage_and_healing_survive_reload,
    test_stored_building_uses_owned_storage_not_gifts,
    test_quest_rewards_pay_once_and_progress_by_order_index,
    test_invalid_quest_id_progress_is_repaired_on_load,
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
