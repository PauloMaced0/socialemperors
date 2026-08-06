import json
import os
import copy
import uuid
import random
from flask import session
# from flask_session import SqlAlchemySessionInterface, current_app

from version import version_code
from engine import timestamp_now
from version import migrate_loaded_save
from constants import Constant

from bundle import VILLAGES_DIR, SAVES_DIR

__villages = {}  # ALL static neighbors
'''__villages = {
    "USERID_1": {
        "playerInfo": {...},
        "maps": [{...},{...}]
        "privateState": {...}
    },
    "USERID_2": {...}
}'''

__saves = {}  # ALL saved villages
'''__saves = {
    "USERID_1": {
        "playerInfo": {...},
        "maps": [{...},{...}]
        "privateState": {...}
    },
    "USERID_2": {...}
}'''

__initial_village = json.load(open(os.path.join(VILLAGES_DIR, "initial.json")))

# Enemy encounters are created by the Flash client and sent back as ordinary
# free ``buy`` commands. These objective-only buildings identify a saved,
# still-active camp.
_ENEMY_CAMP_MARKER_IDS = frozenset({
    Constant.ID_BUILDING_TREASURE_CHEST,
    Constant.ID_BUILDING_PRISONER_PRINCESS,
    Constant.ID_BUILDING_PRISONER_VILLAGERS,
    Constant.ID_BUILDING_PRISONER_ARTHUR,
    Constant.ID_BUILDING_PRISONER_ARCHERS,
    Constant.ID_BUILDING_PRISONER_REBEL_TROLL,
    Constant.ID_BUILDING_POWER_GEM,
    Constant.ID_BUILDING_STATUE_GOLEM,
    Constant.ID_BUILDING_PRISONER_KIDNAPPED_UNITS,
    Constant.ID_BUILDING_TREASURE_TOTEM,
    Constant.ID_BUILDING_TROLL_HUT,
    Constant.ID_BUILDING_TROLL_CAVE,
})

_NATURAL_RESOURCE_IDS = frozenset({
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
})

_DEPLETED_RESOURCE_PLACEHOLDER_IDS = frozenset({
    Constant.ID_BUILDING_REGEN_GOLD,
    Constant.ID_BUILDING_REGEN_STONE,
})

_TREE_RESOURCE_IDS = frozenset({
    Constant.ID_BUILDING_TREE_1,
    Constant.ID_BUILDING_TREE_2,
    Constant.ID_BUILDING_TREE_3,
})

_GOLD_RESOURCE_IDS = frozenset({
    Constant.ID_BUILDING_GOLD_1,
    Constant.ID_BUILDING_GOLD_2,
    Constant.ID_BUILDING_GOLD_3,
    Constant.ID_BUILDING_GOLD_4,
    Constant.ID_BUILDING_REGEN_GOLD,
})

_STONE_RESOURCE_IDS = frozenset({
    Constant.ID_BUILDING_STONE_1,
    Constant.ID_BUILDING_STONE_2,
    Constant.ID_BUILDING_STONE_3,
    Constant.ID_BUILDING_STONE_4,
    Constant.ID_BUILDING_REGEN_STONE,
})

_NATURAL_RESOURCE_RECOVERY_VERSION = 1


def is_enemy_camp_marker(item_id: int) -> bool:
    return int(item_id) in _ENEMY_CAMP_MARKER_IDS


def is_natural_resource(item_id: int) -> bool:
    return int(item_id) in _NATURAL_RESOURCE_IDS


def is_depleted_resource_placeholder(item_id: int) -> bool:
    return int(item_id) in _DEPLETED_RESOURCE_PLACEHOLDER_IDS


def mark_enemy_camp_active(town: dict, now: int = None) -> None:
    """Keep the client respawn gate closed while a saved camp is alive."""
    town["enemyCampActive"] = 1
    town["timestampLastTreasure"] = int(timestamp_now() if now is None else now)


def refresh_enemy_camp_timer(town: dict, now: int) -> bool:
    """A live saved camp must not be replaced at a new random position."""
    if int(town.get("enemyCampActive", 0) or 0):
        now = int(now)
        if int(town.get("timestampLastTreasure", 0) or 0) != now:
            town["timestampLastTreasure"] = now
            return True
    return False


def _repair_active_enemy_camps(save: dict) -> bool:
    """Infer active state for saves made before enemyCampActive existed."""
    changed = False
    for town in save.get("maps", []):
        if "enemyCampActive" in town:
            continue
        if any(is_enemy_camp_marker(item[0])
               for item in town.get("items", []) if item):
            mark_enemy_camp_active(town)
            changed = True
        else:
            town["enemyCampActive"] = 0
            changed = True
    return changed


def _repair_natural_resource_state(save: dict) -> bool:
    """Recognize existing maps and reopen one legacy population pass.

    IDs 80/81 are legacy same-tile gold/stone regeneration placeholders.
    Convert them into server-owned delayed respawns, preserving the remaining
    three-hour cooldown but allowing the replacement deposit to appear at a
    new random wild-map position. Pending tree/mineral respawns still count as
    present, so reloading cannot reopen the client's initial population pass.

    Older server versions permanently removed harvested nodes. Population
    repair is now server-owned in get_player_info so the Flash initializer
    never rerolls every resource position during a reload.
    """
    changed = False
    for town in save.get("maps", []):
        items = town.setdefault("items", [])
        pending_minerals = town.get("pendingMineralRespawns")
        if not isinstance(pending_minerals, list):
            pending_minerals = []
            town["pendingMineralRespawns"] = pending_minerals
            changed = True

        # Migrate saves created by the previous client patch. The placeholder
        # timestamp is when depletion started, so its original remaining
        # cooldown survives both browser and server restarts.
        for item in list(items):
            if not item or int(item[0]) not in _DEPLETED_RESOURCE_PLACEHOLDER_IDS:
                continue
            family = (
                "gold"
                if int(item[0]) == Constant.ID_BUILDING_REGEN_GOLD
                else "stone"
            )
            try:
                source_x, source_y = int(item[1]), int(item[2])
                depleted_at = int(item[4] or 0)
            except (IndexError, TypeError, ValueError):
                items.remove(item)
                changed = True
                continue
            ready_at = depleted_at + Constant.TIMER_RESOURCE_REGEN_SECONDS
            duplicate = False
            for entry in pending_minerals:
                if (
                    not isinstance(entry, dict)
                    or str(entry.get("family")) != family
                ):
                    continue
                try:
                    same_source = (
                        int(entry.get("source_x", -1)) == source_x
                        and int(entry.get("source_y", -1)) == source_y
                    )
                except (TypeError, ValueError):
                    same_source = False
                if same_source:
                    duplicate = True
                    break
            if not duplicate:
                pending_minerals.append({
                    "family": family,
                    "source_x": source_x,
                    "source_y": source_y,
                    "at": ready_at,
                })
            items.remove(item)
            changed = True

        item_ids = {
            int(item[0]) for item in items
            if item
        }
        pending_families = {
            str(entry.get("family"))
            for entry in pending_minerals
            if isinstance(entry, dict)
        }
        has_mineral = bool(
            item_ids & (_GOLD_RESOURCE_IDS | _STONE_RESOURCE_IDS)
            or pending_families & {"gold", "stone"}
        )
        if ("naturalResourcesInitialized" not in town
                and has_mineral):
            town["naturalResourcesInitialized"] = 1
            changed = True

        initialized = int(
            town.get("naturalResourcesInitialized", 0) or 0
        )
        recovery_version = int(
            town.get("naturalResourceRecoveryVersion", 0) or 0
        )
        if recovery_version >= _NATURAL_RESOURCE_RECOVERY_VERSION:
            continue
        if not initialized:
            town["naturalResourcesInitialized"] = 1
        town["naturalResourceRecoveryVersion"] = (
            _NATURAL_RESOURCE_RECOVERY_VERSION
        )
        changed = True
    return changed


def _repair_social_building_state(save: dict) -> bool:
    """Normalize persisted staffing without granting vacant roles for free."""
    from get_game_config import get_game_config

    social_items = {
        int(value["id"]): value
        for value in get_game_config().get("social_items", [])
        if "id" in value
    }
    changed = False
    for town in save.get("maps", []):
        # The stock client used to "open" the first Harbor on every map load:
        # it queued free worker commands, showed Dock operative, and replaced
        # attrs.si with null locally.  There was no durable distinction
        # between those injected zero placeholders and cash-filled roles, so
        # repair the old all-zero state once.  Accepted friend identities are
        # preserved, and buildings staffed after this migration are untouched.
        try:
            harbour_repair_version = int(
                town.get("harbourManualStaffingVersion", 0) or 0
            )
        except (TypeError, ValueError):
            harbour_repair_version = 0
        if harbour_repair_version < 1:
            for item in town.get("items", []):
                if (
                    not item
                    or int(item[0]) != Constant.ID_BUILDING_DOCK
                    or len(item) < 8
                    or not isinstance(item[7], dict)
                ):
                    continue
                attrs = item[7]
                roster = attrs.get("staffRoster")
                auto_opened = (
                    attrs.get("si") is None
                    and (
                        not isinstance(roster, list)
                        or not roster
                        or all(value == 0 for value in roster)
                    )
                )
                if auto_opened:
                    attrs["si"] = []
                    attrs["staffRoles"] = []
                    attrs["staffRoster"] = []
                    changed = True
            town["harbourManualStaffingVersion"] = 1
            changed = True
        for item in town.get("items", []):
            if not item or int(item[0]) not in social_items:
                continue
            social = social_items[int(item[0])]
            roles = [
                role.strip()
                for role in str(social.get("workers", "") or "").split(",")
                if role.strip()
            ]
            while len(item) < 7:
                item.append([])
                changed = True
            if len(item) < 8:
                item.append({"si": []})
                changed = True
            elif not isinstance(item[7], dict):
                item[7] = {"si": []}
                changed = True
            elif "si" not in item[7]:
                item[7]["si"] = []
                changed = True
            attrs = item[7]
            staff = attrs.get("si", [])
            if staff is None:
                # ``si=None`` is an old save's durable proof that every role
                # was completed. Preserve that fact for role-aware upgrades.
                roster = attrs.get("staffRoster")
                roster_roles = attrs.get("staffRoles")
                if not isinstance(roster, list) or len(roster) < len(roles):
                    attrs["staffRoster"] = [0] * len(roles)
                    changed = True
                if not isinstance(roster_roles, list) or roster_roles != roles:
                    attrs["staffRoles"] = list(roles)
                    changed = True
            elif isinstance(staff, list):
                # Partial identities occupy the configured prefix. Recording
                # it makes accepted friend staff survive future upgrades, but
                # does not fill or unlock a single extra position.
                prefix = roles[:min(len(staff), len(roles))]
                roster = list(staff[:len(prefix)])
                if attrs.get("staffRoles") != prefix:
                    attrs["staffRoles"] = prefix
                    changed = True
                if attrs.get("staffRoster") != roster:
                    attrs["staffRoster"] = roster
                    changed = True
    return changed


def _repair_unit_warehouse_state(save: dict) -> bool:
    """Normalize old/missing Unit Warehouse fields without dropping units."""
    changed = False
    for town in save.get("maps", []):
        try:
            capacity = max(0, int(
                town.get("warehouseAditionalCapacitySingle", 0) or 0
            ))
        except (TypeError, ValueError):
            capacity = 0
        if town.get("warehouseAditionalCapacitySingle") != capacity:
            town["warehouseAditionalCapacitySingle"] = capacity
            changed = True

        raw_units = town.get("warehousedUnits", {})
        if isinstance(raw_units, dict):
            units = {}
            for raw_id, raw_count in raw_units.items():
                try:
                    unit_id = str(int(raw_id))
                    count = int(raw_count)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    units[unit_id] = count
        elif isinstance(raw_units, list):
            units = {}
            for raw_id in raw_units:
                try:
                    unit_id = str(int(raw_id))
                except (TypeError, ValueError):
                    continue
                units[unit_id] = units.get(unit_id, 0) + 1
        else:
            units = {}
        if "warehousedUnits" not in town or raw_units != units:
            town["warehousedUnits"] = units
            changed = True
        if (
            capacity < 1
            and any(
                item
                and int(item[0]) == Constant.ID_BUILDING_UNIT_WAREHOUSE
                for item in town.get("items", [])
            )
        ):
            # A newly purchased Warehouse includes one slot; later slots cost
            # the configured 2 cash each.
            town["warehouseAditionalCapacitySingle"] = 1
            changed = True
    return changed


def _repair_monster_nest_state(save: dict) -> bool:
    """Unstick a Monsters Nest left permanently exhausted by the old typo.

    ``desactivate_monster`` used to zero a misspelled ``MonsterNumber``, so the
    real ``monsterNumber`` kept the count of every monster already bred. The
    client compares it against its four nest slots and immediately deactivates
    the nest again, which meant the reactivation popup came back after every
    reload. Drop the junk field and reset an exhausted counter once.
    """
    state = save.get("privateState")
    if not isinstance(state, dict):
        return False
    changed = False
    if "MonsterNumber" in state:
        del state["MonsterNumber"]
        changed = True
    try:
        bred = int(state.get("monsterNumber", 0) or 0)
    except (TypeError, ValueError):
        bred = 0
    # The client's nest holds four monsters (_monster1.._monster4).
    if bred >= 4:
        state["monsterNumber"] = 0
        state["stepMonsterNumber"] = 0
        changed = True
    return changed


def _repair_wounded_units(save: dict) -> bool:
    """Heal units that an older build left damaged forever.

    ``set_item_health`` used to persist ``attrs.hp`` for units as well as
    buildings, but a unit has no way to heal at home, so any scratch taken from
    a troll or an attacking player became permanent. Units now always return
    from a fight at full health; drop the values already written to disk.
    """
    from get_game_config import get_attribute_from_item_id

    changed = False
    for town in save.get("maps", []):
        for item in town.get("items", []):
            if not item or len(item) < 8 or not isinstance(item[7], dict):
                continue
            if "hp" not in item[7]:
                continue
            if str(get_attribute_from_item_id(item[0], "type")) != "u":
                continue
            del item[7]["hp"]
            changed = True
    return changed


def _repair_quest_progress(save: dict) -> bool:
    """Repair the old quest-id/index mix-up without unlocking every island.

    ``unlockedQuestIndex`` is an index into globals.ISLE_ORDER, not a quest
    id. Older server code stored ``quest_id + 1`` (for example 100000007),
    which made every island appear unlocked after one victory. Completed
    ranks are authoritative, so rebuild only the contiguous unlocked prefix
    when the stored index is outside the real list.
    """
    from get_game_config import get_game_config

    order = [
        str(value)
        for value in get_game_config().get("globals", {}).get("ISLE_ORDER", [])
    ]
    if not order:
        return False
    state = save.setdefault("privateState", {})
    try:
        current = int(state.get("unlockedQuestIndex", 0) or 0)
    except (TypeError, ValueError):
        current = -1
    if 0 <= current <= len(order):
        return False
    ranks = state.get("questsRank", {})
    ranks = ranks if isinstance(ranks, dict) else {}
    repaired = 0
    while repaired < len(order) and order[repaired] in ranks:
        repaired += 1
    state["unlockedQuestIndex"] = repaired
    return True

# Load saved villages

def load_saved_villages():
    global __villages
    global __saves
    # Empty in memory
    __villages = {}
    __saves = {}
    # Saves dir check
    if not os.path.exists(SAVES_DIR):
        try:
            print(f"Creating '{SAVES_DIR}' folder...")
            os.mkdir(SAVES_DIR)
        except:
            print(f"Could not create '{SAVES_DIR}' folder.")
            exit(1)
    if not os.path.isdir(SAVES_DIR):
        print(f"'{SAVES_DIR}' is not a folder... Move the file somewhere else.")
        exit(1)
    # Static neighbors in /villages
    for file in os.listdir(VILLAGES_DIR):
        if file == "initial.json" or not file.endswith(".json"):
            continue
        print(f" * Loading static neighbour {file}... ", end='')
        village = json.load(open(os.path.join(VILLAGES_DIR, file)))
        if not is_valid_village(village):
            print("Invalid neighbour")
            continue
        USERID = village["playerInfo"]["pid"]
        if str(USERID) in __villages:
            print(f"Ignored: duplicated PID '{USERID}'.")
        else:
            __villages[str(USERID)] = village
            print("Ok.")
    # Saves in /saves
    for file in os.listdir(SAVES_DIR):
        if not file.endswith(".save.json"):
            continue
        print(f" * Loading save at {file}... ", end='')
        try:
            save = json.load(open(os.path.join(SAVES_DIR, file)))
        except json.decoder.JSONDecodeError as e:
            print("Corrupted JSON.")
            continue
        if not is_valid_village(save):
            print("Invalid Save.")
            continue
        USERID = save["playerInfo"]["pid"]
        try:
            map_name = save["playerInfo"]["map_names"][ save["playerInfo"]["default_map"] ]
        except:
            map_name = '?'
        print(f"({map_name}) Ok.")
        # Scrub transient neighbour-listing fields that older builds leaked
        # into playerInfo (they belong to maps[0] and were persisted by
        # accident via aliasing in neighbors()).
        for leaked in ("coins", "xp", "level", "stone", "wood", "food"):
            save["playerInfo"].pop(leaked, None)
        # Local save files are separate players, not automatically Facebook
        # friends.  Older builds exposed every save as a neighbour, which made
        # private villages appear on friend cards and let social buildings post
        # to people the current player had never added.
        save["privateState"].setdefault("neighbors", [])
        __saves[str(USERID)] = save
        modified = migrate_loaded_save(save) # check save version for migration
        if _repair_broken_troll_towns(save):
            modified = True
        if _repair_active_enemy_camps(save):
            modified = True
        if _repair_natural_resource_state(save):
            modified = True
        if _repair_social_building_state(save):
            modified = True
        if _repair_unit_warehouse_state(save):
            modified = True
        if _repair_quest_progress(save):
            modified = True
        if _repair_wounded_units(save):
            modified = True
        if _repair_monster_nest_state(save):
            modified = True
        if modified:
            save_session(USERID)
    

# New village

def new_village() -> str:
    # Generate USERID
    USERID: str = str(uuid.uuid4())
    assert USERID not in all_userid()
    # Copy init
    village = copy.deepcopy(__initial_village)
    # Custom values
    village["version"] = version_code
    village["playerInfo"]["pid"] = USERID
    village["maps"][0]["timestamp"] = timestamp_now()
    village["privateState"]["dartsRandomSeed"] = abs(int((2**16 - 1) * random.random()))
    # Numeric darts flag from the start (not JSON bool) - the old Flash client
    # misreads a bare `false` as truthy. 0 = no unclaimed free game; the client
    # claims the daily free game itself on a new local day (darts_new_free).
    village["privateState"]["dartsHasFree"] = 0
    village["privateState"]["neighbors"] = []
    # Memory saves
    __saves[USERID] = village
    # Generate save file
    save_session(USERID)
    print("Done.")
    return USERID

# The town hall ITEM decides what the town trains (human hall id 26 trains
# Peasants; Troll Hall I id 289 trains goblins), so a troll town must swap
# the human starter buildings for their troll equivalents. Same subcat, tier
# I. Pre-placed human units are dropped - a fresh town starts with no army
# and the player trains the race's own units.
_HUMAN_TO_TROLL_BUILDING = {
    26: 289,   # Town Hall   -> Troll Hall I
    1: 307,    # House I     -> Troll House I
}
_HUMAN_STARTER_UNITS = {512, 516}  # Light Knight, Light Archer

def fresh_town_map(race: str) -> dict:
    """A brand-new town map for a second-town purchase: the initial town
    layout (town hall, houses, trees) with the chosen race. For a troll town
    the human buildings are swapped for troll ones so the town hall actually
    trains goblins, and the human starter units are dropped."""
    town = copy.deepcopy(__initial_village["maps"][0])
    town["race"] = race
    town["timestamp"] = timestamp_now()
    town["idCurrentTreasure"] = 0
    town["enemyCampActive"] = 0
    # Stamp "just cleared" so the enemy camp doesn't spawn immediately into a
    # brand-new town that has no army yet; it arrives after the normal 4h.
    town["timestampLastTreasure"] = timestamp_now()
    if race == "t":
        new_items = []
        for item in town["items"]:
            item_id = item[0]
            if item_id in _HUMAN_STARTER_UNITS:
                continue  # no pre-placed human army in a troll town
            if item_id in _HUMAN_TO_TROLL_BUILDING:
                item = [_HUMAN_TO_TROLL_BUILDING[item_id]] + item[1:]
            new_items.append(item)
        town["items"] = new_items
    return town

def _repair_broken_troll_towns(save: dict) -> bool:
    """Repair troll towns created by the first buggy buy_map, which cloned the
    human starter (human Town Hall id 26 -> trained Peasants) and then let the
    enemy camp spawn and persist into the fresh town (hundreds of stray items).
    Detect that exact broken state - a troll town still carrying the human town
    hall - and regenerate it as a proper troll town. Runs on load, before any
    save, so a restart fixes it and it can't be clobbered. Returns True if a
    town was rebuilt."""
    changed = False
    for i, town in enumerate(save.get("maps", [])):
        if town.get("race") != "t":
            continue
        ids = {item[0] for item in town.get("items", [])}
        if 26 in ids and 289 not in ids:
            save["maps"][i] = fresh_town_map("t")
            print(f"   > repaired broken troll town (map {i})")
            changed = True
    return changed

# Access functions

def all_saves_userid() -> list:
    "Returns a list of the USERID of every saved village."
    return list(__saves.keys())

def all_userid() -> list:
    "Returns a list of the USERID of every village."
    return list(__villages.keys()) + list(__saves.keys())

def save_info(USERID: str) -> dict:
    from get_game_config import get_level_from_xp
    save = __saves[USERID]
    default_map = save["playerInfo"]["default_map"]
    empire_name = str(save["playerInfo"]["map_names"][default_map])
    xp = save["maps"][default_map]["xp"]
    # Compute level from xp the way the in-game HUD does. The stored `level`
    # field drifts out of sync (e.g. shows 20 for a level-99 xp total), so
    # deriving it keeps the village list consistent with the actual level.
    level = get_level_from_xp(int(xp))
    return{"userid": USERID, "name": empire_name, "xp": xp, "level": level}

def all_saves_info() -> list:
    saves_info = []
    for userid in __saves:
        saves_info.append(save_info(userid))
    return list(saves_info)

def session(USERID: str) -> dict:
    assert(isinstance(USERID, str))
    return __saves[USERID] if USERID in __saves else None

def neighbor_session(USERID: str) -> dict:
    assert(isinstance(USERID, str))
    if USERID in __saves:
        return __saves[USERID]
    if USERID in __villages:
        return __villages[USERID]

def _friend_entry(vill: dict) -> dict:
    """One friendsInfo entry the client indexes by uid. The client reads
    first_name/level/pic_square (friendsInfoMap[uid]) to label neighbour and
    "ask friends to help" cards; without a name those cards render blank."""
    pi = vill["playerInfo"]
    default = int(pi.get("default_map", 0) or 0)
    maps = vill.get("maps") or [{}]
    m = maps[default] if 0 <= default < len(maps) else maps[0]
    pic = pi.get("pic") or "/img/profile/1025.png"
    return {
        "uid": pi["pid"],
        "first_name": _display_name(vill),
        "pic_square": pic,
        "level": m.get("level", 1),
        "xp": m.get("xp", 0),
    }


def _linked_player_ids(USERID: str) -> list:
    """Explicit local-player friendships for ``USERID``.

    Saved player villages only appear when their id is present in
    privateState.neighbors; merely creating another login/save must never
    create a friendship. Static scenario maps are never social users.
    """
    owner = __saves.get(str(USERID))
    if owner is None:
        return []
    raw = owner.get("privateState", {}).get("neighbors", [])
    if not isinstance(raw, list):
        return []
    result = []
    for value in raw:
        uid = str(value)
        if uid != str(USERID) and uid in __saves and uid not in result:
            result.append(uid)
    return result


def is_friend(USERID: str, other_id: str) -> bool:
    """Whether a social action may target ``other_id``."""
    return str(other_id) in _linked_player_ids(str(USERID))


def link_friend(USERID: str, other_id: str) -> bool:
    """Create a reciprocal friendship between two real saved players."""
    USERID, other_id = str(USERID), str(other_id)
    if USERID == other_id or USERID not in __saves or other_id not in __saves:
        return False
    changed = False
    for owner_id, friend_id in ((USERID, other_id), (other_id, USERID)):
        linked = __saves[owner_id]["privateState"].setdefault("neighbors", [])
        if not isinstance(linked, list):
            linked = []
            __saves[owner_id]["privateState"]["neighbors"] = linked
        if friend_id not in [str(value) for value in linked]:
            linked.append(friend_id)
            save_session(owner_id)
            changed = True
    return changed


def unlink_friend(USERID: str, other_id: str) -> bool:
    """Remove a reciprocal friendship without altering completed staffing."""
    USERID, other_id = str(USERID), str(other_id)
    if USERID not in __saves or other_id not in __saves:
        return False
    changed = False
    for owner_id, friend_id in ((USERID, other_id), (other_id, USERID)):
        linked = __saves[owner_id]["privateState"].get("neighbors", [])
        if not isinstance(linked, list):
            continue
        filtered = [value for value in linked if str(value) != friend_id]
        if filtered != linked:
            __saves[owner_id]["privateState"]["neighbors"] = filtered
            save_session(owner_id)
            changed = True
    return changed


def _friend_request_list(owner_id: str) -> list:
    """The owner's incoming friend-request queue (requester uids)."""
    ps = __saves[owner_id]["privateState"]
    reqs = ps.get("friendRequests")
    if not isinstance(reqs, list):
        reqs = []
        ps["friendRequests"] = reqs
    return reqs


def request_friend(USERID: str, other_id: str) -> str:
    """Send a local friend request. Real Facebook had an async invite/accept
    handshake; recreate it between saved players. A request sits in the target's
    queue until accepted. If the target already requested us, accept at once.
    Returns a status code: requested / accepted / pending / already_friends /
    invalid."""
    USERID, other_id = str(USERID), str(other_id)
    if USERID == other_id or USERID not in __saves or other_id not in __saves:
        return "invalid"
    if is_friend(USERID, other_id):
        return "already_friends"
    # They already asked us -> accepting our own send completes the link.
    if other_id in [str(value) for value in _friend_request_list(USERID)]:
        accept_friend(USERID, other_id)
        return "accepted"
    target_queue = _friend_request_list(other_id)
    if USERID in [str(value) for value in target_queue]:
        return "pending"
    target_queue.append(USERID)
    save_session(other_id)
    return "requested"


def accept_friend(USERID: str, other_id: str) -> bool:
    """Accept an incoming request: create the reciprocal link and clear the
    request from both queues."""
    USERID, other_id = str(USERID), str(other_id)
    if USERID not in __saves or other_id not in __saves:
        return False
    if other_id not in [str(value) for value in _friend_request_list(USERID)]:
        return False
    link_friend(USERID, other_id)
    for owner_id, requester_id in ((USERID, other_id), (other_id, USERID)):
        queue = _friend_request_list(owner_id)
        filtered = [value for value in queue if str(value) != requester_id]
        if filtered != queue:
            __saves[owner_id]["privateState"]["friendRequests"] = filtered
            save_session(owner_id)
    return True


def decline_friend(USERID: str, other_id: str) -> bool:
    """Remove an incoming request without linking."""
    USERID, other_id = str(USERID), str(other_id)
    if USERID not in __saves:
        return False
    queue = _friend_request_list(USERID)
    filtered = [value for value in queue if str(value) != other_id]
    if filtered == queue:
        return False
    __saves[USERID]["privateState"]["friendRequests"] = filtered
    save_session(USERID)
    return True


def incoming_friend_requests(USERID: str) -> list:
    """Players who have asked to be this player's friend and are not linked yet."""
    USERID = str(USERID)
    if USERID not in __saves:
        return []
    linked = set(_linked_player_ids(USERID))
    requests = []
    for uid in list(_friend_request_list(USERID)):
        uid = str(uid)
        if uid in __saves and uid != USERID and uid not in linked:
            requests.append(save_info(uid))
    return requests


def friend_candidates(USERID: str) -> list:
    """Saved players available to add through the local Friends page."""
    USERID = str(USERID)
    linked = set(_linked_player_ids(USERID))
    candidates = []
    for uid in sorted(__saves):
        if uid == USERID:
            continue
        info = save_info(uid)
        info["linked"] = uid in linked
        # A pending outgoing request means our id sits in their inbox.
        info["requested"] = (
            not info["linked"]
            and USERID in [str(value) for value in _friend_request_list(uid)]
        )
        candidates.append(info)
    return candidates


def fb_friends_str(USERID: str) -> list:
    friends = []
    # Explicitly linked local players only. Static scenarios and unrelated
    # save files are not Facebook friends.
    for key in _linked_player_ids(USERID):
        friends += [_friend_entry(__saves[key])]
    return friends

def _display_name(vill: dict) -> str:
    """The name shown on player/neighbour cards.

    Two kinds of village disagree on where the real name lives:
      - Player saves keep playerInfo["name"] at the hardcoded default
        "Emperor" and carry the real identity in map_names (the town name).
      - Static scenario maps have a personalised
        playerInfo["name"] but a generic map_names ("My Empire", "Boss").
    So trust playerInfo["name"] when it's been personalised, and only fall
    back to the default town's name when it's still the generic "Emperor"."""
    pi = vill["playerInfo"]
    # Player saves use the empire/town name as their only locally editable
    # identity.  Always read it live, so renaming a town also fixes cards that
    # previously remained stuck on "Emperor" or an older name.
    if str(pi.get("pid")) in __saves:
        names = pi.get("map_names") or []
        dm = int(pi.get("default_map", 0) or 0)
        if 0 <= dm < len(names) and names[dm]:
            return str(names[dm])
    name = pi.get("name")
    if name and name != "Emperor":
        return str(name)
    names = pi.get("map_names") or []
    dm = int(pi.get("default_map", 0) or 0)
    if 0 <= dm < len(names) and names[dm]:
        return str(names[dm])
    return name or "Emperor"


def neighbors(USERID: str) -> list:
    neighbors = []
    # Explicitly linked local players only.
    for key in _linked_player_ids(USERID):
        vill = __saves[key]
        neigh = dict(vill["playerInfo"])
        neigh["name"] = _display_name(vill)
        neigh["coins"] = vill["maps"][0]["coins"]
        neigh["xp"] = vill["maps"][0]["xp"]
        neigh["level"] = vill["maps"][0]["level"]
        neigh["stone"] = vill["maps"][0]["stone"]
        neigh["wood"] = vill["maps"][0]["wood"]
        neigh["food"] = vill["maps"][0]["food"]
        neighbors += [neigh]
    return neighbors


def pvp_profiles() -> list:
    """Serializable player profiles used by the PvP continent.

    PvP opponents are deliberately independent from friendship: all saved
    villages can be matched in battle without being injected into the social
    neighbour bar.
    """
    from get_game_config import get_level_from_xp

    profiles = []
    # PvP is between real saved players. Static scenario maps are never
    # surfaced as accounts or opponents.
    candidates = sorted(__saves.items(), key=lambda pair: pair[0])
    for uid, vill in candidates:
        pi = vill["playerInfo"]
        maps = vill.get("maps") or [{}]
        dm = int(pi.get("default_map", 0) or 0)
        if dm < 0 or dm >= len(maps):
            dm = 0
        town = maps[dm]
        profiles.append({
            "user_id": str(uid),
            "name": _display_name(vill),
            "level": max(1, int(get_level_from_xp(int(town.get("xp", 0) or 0)))),
            "race": town.get("race", "h"),
            "map": dm,
            "pic": pi.get("pic") or "",
        })
    return profiles

# Check for valid village
# The reason why this was implemented is to warn the user if a save game from Social Wars was used by accident

def is_valid_village(save: dict):
    if "playerInfo" not in save or "maps" not in save or "privateState" not in save:
        # These are obvious
        return False
    for map in save["maps"]:
        if "oil" in map or "steel" in map:
            return False
        if "stone" not in map or "food" not in map:
            return False
        if "items" not in map:
            return False
        if type(map["items"]) != list:
            return False

    return True

# Persistency

def backup_session(USERID: str):
    # TODO 
    return

def save_session(USERID: str):
    file = f"{USERID}.save.json"
    print(f" * Saving village at {file}... ", end='')
    village = session(USERID)
    # Atomic write: dump to a temp file in the same dir, then replace the target.
    # A crash mid-write cannot corrupt an existing save.
    target = os.path.join(SAVES_DIR, file)
    tmp = target + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(village, f, indent=4)
    os.replace(tmp, target)
    print("Done.")
