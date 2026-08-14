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
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import command
import engine
from constants import Constant
from get_game_config import get_game_config

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


def test_concurrent_saves_use_independent_temp_files(tmp):
    """Two Flask requests saving one village cannot steal one .tmp file."""
    original_dump = sessions.json.dump
    both_dumped = threading.Barrier(2)
    errors = []

    def synchronized_dump(*args, **kwargs):
        original_dump(*args, **kwargs)
        both_dumped.wait(timeout=5)

    def save_once():
        try:
            sessions.save_session(UID)
        except Exception as exc:  # surfaced below with the original type/text
            errors.append(exc)

    sessions.json.dump = synchronized_dump
    try:
        threads = [threading.Thread(target=save_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads), \
            "concurrent village saves deadlocked"
    finally:
        sessions.json.dump = original_dump

    assert not errors, [f"{type(exc).__name__}: {exc}" for exc in errors]
    leftovers = [name for name in os.listdir(tmp) if name.endswith(".tmp")]
    assert not leftovers, f"concurrent saves left temp files: {leftovers}"
    json.load(open(os.path.join(tmp, f"{UID}.save.json")))


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


def test_darts_prize_packet_is_one_time_across_reload(tmp):
    """A retried darts packet must not put its troop back into Gifts.

    CommandManager retries an in-flight HTTP packet when the response is lost.
    The persisted balloon index is the authoritative idempotency key.
    """
    unit_id = 725  # Black Draggy
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 100
    ps = save["privateState"]
    ps["gifts"] = []
    ps["dartsBalloonsShot"] = []
    ps["dartsHasFree"] = True
    packet = _batch([
        {"cmd": Constant.CMD_STORE_ADD_ITEMS,
         "args": [json.dumps([unit_id])]},
        {"cmd": Constant.CMD_DARTS_SHOOT_BALLOON,
         "args": [4, 0, 0]},
    ])
    command.command(UID, packet)
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    assert disk["privateState"]["gifts"][unit_id] == 1
    assert disk["privateState"]["dartsBalloonsShot"] == [4]

    # Simulate a browser/server reload, consume the gift, then replay the old
    # darts request.  The unit must stay deployed rather than returning.
    sessions.load_saved_villages()
    command.command(UID, _batch([
        {"cmd": Constant.CMD_PLACE_GIFT,
         "args": [unit_id, 44, 45, 0, 0]},
    ]))
    sessions.load_saved_villages()
    command.command(UID, packet)
    sessions.load_saved_villages()
    reloaded = sessions.session(UID)
    gifts = reloaded["privateState"]["gifts"]
    assert len(gifts) <= unit_id or gifts[unit_id] == 0, \
        "a retried darts reward put the deployed troop back into Gifts"
    deployed = [
        item for item in reloaded["maps"][0]["items"]
        if item[0] == unit_id and item[1] == 44 and item[2] == 45
    ]
    assert len(deployed) == 1, "gift placement was lost or duplicated"
    assert reloaded["privateState"]["dartsBalloonsShot"] == [4]
    assert reloaded["playerInfo"]["cash"] == 100, \
        "duplicate free throw was incorrectly converted into a paid throw"


def test_darts_final_prize_group_is_atomic_and_one_time(tmp):
    """The 25th balloon stores its normal and bonus units exactly once."""
    normal, bonus = 725, 771
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 100
    ps = save["privateState"]
    ps["gifts"] = []
    ps["dartsBalloonsShot"] = list(range(24))
    ps["dartsHasFree"] = True
    ps["dartsGotExtra"] = False
    packet = _batch([
        {"cmd": Constant.CMD_STORE_ADD_ITEMS,
         "args": [json.dumps([normal])]},
        {"cmd": Constant.CMD_STORE_ADD_ITEMS,
         "args": [json.dumps([bonus])]},
        {"cmd": Constant.CMD_DARTS_SHOOT_BALLOON,
         "args": [24, 0, 1]},
    ])
    command.command(UID, packet)
    command.command(UID, packet)
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    gifts = disk["privateState"]["gifts"]
    assert gifts[normal] == 1 and gifts[bonus] == 1, \
        "final balloon prize group was partly lost or duplicated"
    assert disk["privateState"]["dartsBalloonsShot"] == list(range(25))
    assert disk["privateState"]["dartsGotExtra"] is True


def test_sacrifice_altar_collection_resets_and_persists(tmp):
    """pop_sell consumes the altar unit and cannot reward it twice."""
    altar_id, unit_id = Constant.ID_BUILDING_SACRIFICE, 515
    save = sessions.session(UID)
    town = save["maps"][0]
    town["coins"] = 0
    town["items"].append([
        altar_id, 68, 77, 0, OLD_TS, 0, [unit_id], {}
    ])
    packet = _batch([
        {"cmd": Constant.CMD_POP_SELL,
         "args": [68, 77, 0, altar_id, unit_id]},
    ])
    command.command(UID, packet)
    sessions.load_saved_villages()
    reloaded = sessions.session(UID)
    altar = next(
        item for item in reloaded["maps"][0]["items"]
        if item[0] == altar_id and item[1] == 68 and item[2] == 77
    )
    assert altar[6] == [], "sacrificed unit returned to the altar after reload"
    assert altar[4] == 0, "completed altar timer was not reset"
    # Medium Archer Knight costs 300 gold; the client awards floor(300/20).
    assert reloaded["maps"][0]["coins"] == 15, \
        "altar's visible gold reward did not persist"
    command.command(UID, packet)
    assert sessions.session(UID)["maps"][0]["coins"] == 15, \
        "replaying pop_sell rewarded the same sacrifice twice"


def test_darts_new_free_stamps_claim(tmp):
    """Claiming the daily free stamps timeStampDartsNewFree so it can't be reclaimed."""
    command.command(UID, _batch([{"cmd": Constant.CMD_DARTS_NEW_FREE, "args": []}]))
    now = engine.timestamp_now()
    ts = sessions.session(UID)["privateState"]["timeStampDartsNewFree"]
    assert now - 10 <= ts <= now, f"free-claim timestamp not set: {ts}"


def test_unit_collection_completed_grants_cash_once(tmp):
    """Completing a unit collection marks it done and grants +1 cash, once."""
    save = sessions.session(UID)
    ps = save["privateState"]
    ps["unitCollectionsCompleted"] = []
    save["playerInfo"]["cash"] = 0
    categories = get_game_config()["units_collections_categories"]
    save["maps"][0]["store"] = {
        str(unit_id): 1
        for collection_id in (5, 6)
        for unit_id in categories[str(collection_id)]["units"]
    }
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [5]}]))
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    assert 5 in disk["privateState"]["unitCollectionsCompleted"], "collection not marked done"
    assert disk["playerInfo"]["cash"] == 1, f"cash reward not granted: {disk['playerInfo']['cash']}"
    # re-sending the same collection must not grant cash again
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [5]}]))
    assert sessions.session(UID)["playerInfo"]["cash"] == 1, "cash double-granted"
    command.command(UID, _batch([{"cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED, "args": [6]}]))
    assert sessions.session(UID)["playerInfo"]["cash"] == 2, "second collection cash missing"


def test_unit_collection_uses_durable_discoveries_not_current_army(tmp):
    save = sessions.session(UID)
    required = [
        int(value) for value in
        get_game_config()["units_collections_categories"]["5"]["units"]
    ]
    save["maps"][0]["store"] = {str(value): 1 for value in required}
    sessions.save_session(UID)
    assert set(required).issubset(save["privateState"]["boughtUnits"])

    # Once discovered, removing every physical copy must not erase the book.
    save["maps"][0]["store"] = {}
    sessions.save_session(UID)
    sessions.load_saved_villages()
    restored = sessions.session(UID)
    assert set(required).issubset(restored["privateState"]["boughtUnits"])

    restored["privateState"]["unitCollectionsCompleted"] = []
    restored["playerInfo"]["cash"] = 0
    command.command(UID, _batch([{
        "cmd": Constant.CMD_UNIT_COLLECTION_COMPLETED,
        "args": [5],
    }]))
    assert restored["playerInfo"]["cash"] == 1


def test_hostile_tutorial_units_are_not_discovered(tmp):
    save = sessions.session(UID)
    save["privateState"]["boughtUnits"] = []
    sessions.sync_discovered_units(save)
    assert 512 in save["privateState"]["boughtUnits"]
    assert 516 in save["privateState"]["boughtUnits"]
    assert 525 not in save["privateState"]["boughtUnits"], \
        "the tutorial Small Troll was mistaken for a player discovery"


def test_collection_book_unit_purchase_persists_and_uses_config_price(tmp):
    save = sessions.session(UID)
    save["playerInfo"]["cash"] = 100
    save["maps"][0]["store"] = {}
    # Heavy Draggy is the first Draggies II item. A malformed legacy [null]
    # override used to show/pay zero; the category's intended price is 15.
    command.command(UID, _batch([{
        "cmd": Constant.CMD_BUY_STORED_ITEM_CASH,
        "args": [0, 768, 0],
    }]))
    sessions.load_saved_villages()
    restored = sessions.session(UID)
    assert restored["playerInfo"]["cash"] == 85
    assert restored["maps"][0]["store"]["768"] == 1
    assert 768 in restored["privateState"]["boughtUnits"]


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
    test_unit_collection_uses_durable_discoveries_not_current_army,
    test_hostile_tutorial_units_are_not_discovered,
    test_collection_book_unit_purchase_persists_and_uses_config_price,
    test_apply_rewards_ranking_grants_cash_and_items,
    test_darts_flow_persists,
    test_darts_prize_packet_is_one_time_across_reload,
    test_darts_final_prize_group_is_atomic_and_one_time,
    test_sacrifice_altar_collection_resets_and_persists,
    test_darts_new_free_stamps_claim,
    test_store_item_frombug_moves_item_to_storage,
    test_end_quest_win_saves_star_rank,
    test_end_attack_win_marks_conquered_and_rewards,
    test_end_attack_loss_does_not_mark_conquered,
    test_collect_advances_item_timestamp,
    test_batch_persists_despite_failing_command,
    test_atomic_save_leaves_no_tmp_file,
    test_concurrent_saves_use_independent_temp_files,
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
