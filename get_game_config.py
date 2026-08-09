import json
import os
import datetime
import jsonpatch
from bundle import MODS_DIR, CONFIG_DIR, CONFIG_PATCH_DIR

__game_config = json.load(open(os.path.join(CONFIG_DIR, "game_config_20120826.json"), 'r', encoding='utf-8'))

def remove_duplicate_items():
    indexes = {}
    items = __game_config["items"]
    num_duplicate = 0

    while True:
        index = 0
        duplicate = False
        for item in items:
            if item["id"] in indexes:
                del items[indexes[item["id"]]]
                indexes.clear()
                duplicate = True
                num_duplicate += 1
                break

            indexes[item["id"]] = index
            index += 1

        if duplicate:
            continue
        
        if num_duplicate:
            print(f" * Removed {num_duplicate} duplicate items from config patches")
        break

def apply_config_patch(filename):
    patch = json.load(open(filename, 'r'))
    jsonpatch.apply_patch(__game_config, patch, in_place=True)

def patch_game_config():

    # Apply patches

    for patch_file in os.listdir(CONFIG_PATCH_DIR):
        if patch_file.endswith(".json"):
            f = os.path.join(CONFIG_PATCH_DIR, patch_file)
            apply_config_patch(f)
            patch = patch_file.replace(".json", "")
            print(" * Patch applied:", patch)

    # Apply mods

    if os.path.exists(MODS_DIR + "/mods.txt"):
        with open(MODS_DIR + "/mods.txt", "r") as f:
            lines = f.readlines()
            f.close()

        for line in lines:
            mod = line.strip()
            if mod.startswith("#"):
                continue
            if mod != "":
                mod.replace(".json", "")
                mod_path = f"{MODS_DIR}/{mod}.json"
                if os.path.exists(mod_path):
                    apply_config_patch(mod_path)
                    print(" * Mod applied:", mod)

    remove_duplicate_items()

print (" [+] Applying config patches and mods...")
patch_game_config()

def refresh_darts_schedule() -> None:
    """Repoint the most recent weekly darts prize set to the current week.

    The bundled darts_items schedule runs out in 2012, so the client can find
    no darts set for "this week" and shows "Come back next week for new
    prizes!", making the daily darts unplayable. Anchoring the latest set to
    today keeps a darts game permanently available; player progress lives in
    privateState (dartsBalloonsShot), not here, so this does not wipe it.
    """
    darts = __game_config.get("darts_items")
    if not darts:
        return
    # Index of the last set that actually carries prizes (set 33 is an empty
    # terminator with no items).
    playable_idx = [i for i, e in enumerate(darts) if e.get("items")]
    if not playable_idx:
        return
    last_real = playable_idx[-1]
    # Rebase the ENTIRE weekly schedule onto real dates, anchoring the last
    # real prize set to the current week. Past sets fall on prior weeks and any
    # trailing (empty) set lands next week. This keeps a darts game available
    # regardless of whether the client selects "this week" by latest
    # start_date <= now or by weeks elapsed since the first set.
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    for i, e in enumerate(darts):
        wk = monday + datetime.timedelta(weeks=(i - last_real))
        e["start_date"] = wk.strftime("%Y-%m-%d 00:00:00")

def fix_resource_upgrades() -> None:
    """Repair broken resource-building progression in the bundled config.

    The Mill, Gold Mine and Stone Mine tier chains ship with upgrades_to=0, so
    the client shows no "upgrade" button on them - unlike Lumber Mill, which is
    correctly linked I->II->III->IV. Wire each tier to the next by id so they
    upgrade like every other building. (Troll equivalents are already linked.)

    The human crop chain is also out of order: Eggplant Field points back to
    the weaker level-1 Farm Land. Its intended order follows both unlock level
    and yield. Finally, Troll Mill II accidentally costs zero instead of the
    same 200 wood as the human tier-II mill.
    """
    chains = [
        ["5", "6", "7", "202"],     # Mill I -> II -> III -> IV
        ["13", "14", "15", "203"],  # Gold Mine I -> II -> III -> IV
        ["16", "17", "18", "204"],  # Stone Mine I -> II -> III -> IV
        ["10", "8", "9", "200", "201"],  # Farm -> Pumpkin -> Eggplant -> Carrot -> Watermelon
    ]
    by_id = {str(it.get("id")): it for it in __game_config.get("items", [])}
    for chain in chains:
        for lower, higher in zip(chain, chain[1:]):
            it = by_id.get(lower)
            if it is not None:
                it["upgrades_to"] = higher
    troll_mill_ii = by_id.get("301")
    if troll_mill_ii is not None:
        troll_mill_ii["cost"] = "200"

fix_resource_upgrades()


def _social_worker_roles(social: dict) -> list[str]:
    return [
        role.strip()
        for role in str(social.get("workers", "") or "").split(",")
        if role.strip()
    ]


def fix_social_upgrade_roles() -> None:
    """Keep inherited staff roles at the front of every upgraded tier.

    The Flash staffing popup stores a packed array: slot zero always belongs
    to the first configured worker name.  Some higher tiers list their old
    jobs in a different order (notably human Stone Mine III), which made a
    simple carry-over turn a Geologist into a Cartographer.  Reordering the
    *same* target role set lets a staffed lower tier retain matching employees
    while leaving only genuinely new jobs vacant.
    """
    items = {
        int(item["id"]): item
        for item in __game_config.get("items", [])
        if "id" in item
    }
    social = {
        int(item["id"]): item
        for item in __game_config.get("social_items", [])
        if "id" in item
    }
    predecessors = {}
    for item_id, item in items.items():
        try:
            target = int(item.get("upgrades_to", 0) or 0)
        except (TypeError, ValueError):
            target = 0
        if target:
            predecessors[target] = item_id

    for target_id, target_social in social.items():
        previous_id = predecessors.get(target_id)
        seen = set()
        while (
            previous_id is not None
            and previous_id not in social
            and previous_id not in seen
        ):
            seen.add(previous_id)
            previous_id = predecessors.get(previous_id)
        if previous_id not in social:
            continue

        previous_roles = _social_worker_roles(social[previous_id])
        remaining = _social_worker_roles(target_social)
        inherited = []
        for role in previous_roles:
            normalized = role.casefold()
            match = next((
                index for index, candidate in enumerate(remaining)
                if candidate.casefold() == normalized
            ), None)
            if match is not None:
                inherited.append(remaining.pop(match))
        target_social["workers"] = ",".join(inherited + remaining)


fix_social_upgrade_roles()


def fix_tournament_economy() -> None:
    """Apply the local tournament cash rebalance requested for the arena.

    Advanced (type 2) is deliberately the reference tier and stays at its
    original 15 cash entry / 30 cash prize.  The two other cash brackets were
    prohibitively expensive on a small local server, so their entry and cash
    prize are both halved.  Keeping the same 2:1 payout ratio avoids changing
    the risk/reward balance while making those brackets usable.
    """
    tournaments = __game_config.get("tournament_type", {})
    for type_id in ("4", "6"):
        definition = tournaments.get(type_id)
        if not definition:
            continue
        definition["cost"] = int(definition.get("cost", 0) or 0) // 2
        for prize in definition.get("prize", []):
            if "c" in prize:
                prize["c"] = int(prize.get("c", 0) or 0) // 2

    # A weekly bracket on this local server contains the player and the three
    # configured arena bots.  Keeping the original top-ten reward table would
    # award every entrant a rare dragon automatically.  Expose only the
    # first-place prize; the service also requires all three matches and rank
    # one before crediting it.
    weekly = tournaments.get("8")
    if weekly and weekly.get("prize"):
        weekly["prize"] = weekly["prize"][:1]


fix_tournament_economy()


def get_game_config() -> dict:
    return __game_config

def game_config() -> dict:
    return get_game_config()

##########
# PLAYER #
##########

def get_xp_from_level(level: int) -> int:
    return __game_config["levels"][int(level)]["exp_required"]

def get_level_from_xp(xp: int) -> int:
    i = 0
    for lvl in __game_config["levels"]:
        if lvl["exp_required"] > int(xp):
            return i
        i += 1
    return 0

#########
# ITEMS #
#########

# ID

items_dict_id_to_items_index = {int(item["id"]): i for i, item in enumerate(__game_config["items"])}

def get_item_from_id(id: int) -> dict:
    items_index = items_dict_id_to_items_index[int(id)] if int(id) in items_dict_id_to_items_index else None
    return __game_config["items"][items_index] if items_index is not None else None

def get_attribute_from_item_id(id: int, attribute_name: str) -> str:
    item = get_item_from_id(id)
    return item[attribute_name] if item and attribute_name in item else None

def get_name_from_item_id(id: int) -> str:
    return get_attribute_from_item_id(id, "name")

# subcat_functional

items_dict_subcat_functional_to_items_index = {int(item["subcat_functional"]): i for i, item in enumerate(__game_config["items"])}

def get_item_from_subcat_functional(subcat_functional: int) -> dict:
    items_index = items_dict_subcat_functional_to_items_index[int(subcat_functional)] if int(subcat_functional) in items_dict_subcat_functional_to_items_index else None
    return __game_config["items"][items_index] if items_index is not None else None

############
# MISSIONS #
############

missions_dict_id_to_missions_index = {int(item["id"]): i for i, item in enumerate(__game_config["missions"])}

def get_mission_from_id(id: int) -> dict:
    items_index = missions_dict_id_to_missions_index[int(id)] if int(id) in missions_dict_id_to_missions_index else None
    return __game_config["missions"][items_index] if items_index is not None else None

def get_attribute_from_mission_id(id: int, attribute_name: str) -> str:
    mission = get_mission_from_id(id)
    return mission[attribute_name] if mission and attribute_name in mission else None
