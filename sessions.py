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
        __saves[str(USERID)] = save
        modified = migrate_loaded_save(save) # check save version for migration
        if _repair_broken_troll_towns(save):
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

def fb_friends_str(USERID: str) -> list:
    DELETE_ME = [{"uid": "1111", "pic_square":"http://127.0.0.1:5050/img/profile/Paladin_Justiciero.jpg"},
        {"uid": "aa_002", "pic_square":"/1025.png"}]
    friends = []
    # static villages
    for key in __villages:
        vill = __villages[key]
        # Avoid Arthur being loaded as friend.
        if vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_1 \
        or vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_2 \
        or vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_3:
            continue
        frie = {}
        frie["uid"] = vill["playerInfo"]["pid"]
        frie["pic_square"] = vill["playerInfo"]["pic"]
        if not frie["pic_square"]: frie["pic_square"] = "/img/profile/1025.png"
        friends += [frie]
    # other players
    for key in __saves:
        vill = __saves[key]
        if vill["playerInfo"]["pid"] == USERID:
            continue
        frie = {}
        frie["uid"] = vill["playerInfo"]["pid"]
        frie["pic_square"] = vill["playerInfo"]["pic"]
        if not frie["pic_square"]: frie["pic_square"] = "/img/profile/1025.png"
        friends += [frie]
    return friends

def _display_name(vill: dict) -> str:
    """The name shown on player/neighbour cards.

    Two kinds of village disagree on where the real name lives:
      - Player saves keep playerInfo["name"] at the hardcoded default
        "Emperor" and carry the real identity in map_names (the town name).
      - Static neighbours (AcidCaos, Arthur) have a personalised
        playerInfo["name"] but a generic map_names ("My Empire", "Boss").
    So trust playerInfo["name"] when it's been personalised, and only fall
    back to the default town's name when it's still the generic "Emperor"."""
    pi = vill["playerInfo"]
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
    # static villages
    for key in __villages:
        vill = __villages[key]
        # Avoid Arthur being loaded as multiple neigtbors.
        if vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_1 \
        or vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_2 \
        or vill["playerInfo"]["pid"] == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_3:
            continue
        # Copy: mutating playerInfo in place leaks these transient fields
        # into the stored village and gets persisted on the next save.
        neigh = dict(vill["playerInfo"])
        neigh["name"] = _display_name(vill)
        neigh["coins"] = vill["maps"][0]["coins"]
        neigh["xp"] = vill["maps"][0]["xp"]
        neigh["level"] = vill["maps"][0]["level"]
        neigh["stone"] = vill["maps"][0]["stone"]
        neigh["wood"] = vill["maps"][0]["wood"]
        neigh["food"] = vill["maps"][0]["food"]
        neighbors += [neigh]
    # other players
    for key in __saves:
        vill = __saves[key]
        if vill["playerInfo"]["pid"] == USERID:
            continue
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