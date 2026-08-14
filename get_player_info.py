import copy
import datetime
import random

from sessions import (
    session, save_session, neighbor_session, neighbors,
    refresh_enemy_camp_timer, _display_name, _repair_social_building_state,
    _repair_natural_resource_state, _repair_active_enemy_camps,
    _repair_tutorial_progress, _repair_tutorial_targets,
    _tutorial_is_incomplete,
    _ENEMY_CAMP_MARKER_IDS,
)
from engine import timestamp_now
from constants import Constant
from get_game_config import get_attribute_from_item_id, get_level_from_xp


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _ensure_town_list(save):
    """Populate privateState.maps, the town list the client feeds to
    TownManager.init(). TownManager.hasSecondTown() does `maps.length > 1`
    and getSecondRace() reads `maps[1].r`, so a missing field makes the
    client crash (#1009 null.length) the moment the player opens the Town /
    race-selector popup - the screen dims and nothing happens.

    Each entry is one owned town carrying its race code ("h"/"t"). Rebuilt
    from the authoritative maps array on every load so it can never drift."""
    save["privateState"]["maps"] = [
        {"r": m.get("race", "h")} for m in save["maps"]
    ]


def _sync_global_level(save):
    """Mirror the player's global xp/level onto every town. The client shows
    the CURRENT town's map.xp/level as the player level, and levelling sends
    no town id (level is a single per-account value in the original game). If
    towns kept independent xp, entering a fresh second town would show its
    low level and levelling there would overwrite the main town. Keep the
    default town as canonical and copy its xp/level to the others."""
    maps = save["maps"]
    default = int(save["playerInfo"].get("default_map", 0) or 0)
    if default < 0 or default >= len(maps):
        default = 0
    canonical = maps[default]
    xp = max(0, int(canonical.get("xp", 0) or 0))
    # The level field in old saves can drift from XP.  The HUD uses both
    # values to choose its min/max thresholds, so normalize the level before
    # the client computes the within-level progress bar.
    level = max(1, int(get_level_from_xp(xp)))
    canonical["xp"], canonical["level"] = xp, level
    for town in maps:
        if town is not canonical:
            town["xp"], town["level"] = xp, level


def _same_local_day(ts_a, ts_b):
    if int(ts_a or 0) <= 0 or int(ts_b or 0) <= 0:
        return False
    return datetime.date.fromtimestamp(int(ts_a)) == datetime.date.fromtimestamp(int(ts_b))


def _refresh_daily_animal_budget(save, now):
    """Reset the client's per-subcategory spawn allowance once per local day.

    Existing animals remain persisted map items, so the client only creates
    animals which are actually missing, capped by ANIMALS_PER_DAY.
    """
    changed = False
    pstate = save["privateState"]
    last = int(pstate.get("timestampAnimalsReset", 0) or 0)
    if last > 0 and not _same_local_day(last, now):
        pstate["arrayAnimals"] = {}
        changed = True
    if not isinstance(pstate.get("arrayAnimals"), dict):
        pstate["arrayAnimals"] = {}
        changed = True
    if last != int(now):
        pstate["timestampAnimalsReset"] = int(now)
        changed = True
    return changed


def _respawn_mature_trees(save, now):
    """Restore harvested trees after the natural-resource cooldown.

    Trees have no visible regeneration object, so their cooldown is persisted
    here. Once mature, a tree receives one new random empty wild tile. That
    chosen position is persisted, so later browser reloads cannot move it
    again. This is distinct from rerolling resources on every page load.
    """
    changed = False
    tree_ids = {
        Constant.ID_BUILDING_TREE_1,
        Constant.ID_BUILDING_TREE_2,
        Constant.ID_BUILDING_TREE_3,
    }
    for town in save.get("maps", []):
        pending = town.get("pendingTreeRespawns")
        if not isinstance(pending, list) or not pending:
            continue
        items = town.setdefault("items", [])
        occupied_tiles = _occupied_map_tiles(town)
        tree_count = sum(
            1 for item in items
            if item and int(item[0]) in tree_ids
        )
        remaining = []
        ready = []
        for entry in pending:
            try:
                tree_id = int(entry["id"])
                x, y = int(entry["x"]), int(entry["y"])
                ready_at = int(entry["at"])
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if tree_id not in tree_ids:
                changed = True
                continue
            if int(now) < ready_at:
                remaining.append(entry)
                continue
            ready.append({
                "entry": entry,
                "id": tree_id,
                "source_x": x,
                "source_y": y,
            })
        available = max(0, 300 - tree_count)
        spawnable, overflow = ready[:available], ready[available:]
        remaining.extend(value["entry"] for value in overflow)
        visible_positions = [
            (int(item[1]), int(item[2]))
            for item in items
            if item and int(item[0]) in tree_ids and len(item) >= 3
        ]
        positions = _clustered_respawn_positions(
            town, occupied_tiles, spawnable, visible_positions
        )
        for value, position in zip(spawnable, positions):
            if position is not None:
                items.append([
                    value["id"], position[0], position[1], 0, int(now), 0
                ])
                tree_count += 1
                changed = True
            else:
                # A full map is temporary: keep the matured timer and retry
                # on a later load rather than losing this tree permanently.
                remaining.append(value["entry"])
        if remaining != pending:
            town["pendingTreeRespawns"] = remaining
    return changed


_MINERAL_RESPAWN_IDS = {
    "gold": (
        Constant.ID_BUILDING_GOLD_1,
        Constant.ID_BUILDING_GOLD_2,
        Constant.ID_BUILDING_GOLD_3,
        Constant.ID_BUILDING_GOLD_4,
    ),
    "stone": (
        Constant.ID_BUILDING_STONE_1,
        Constant.ID_BUILDING_STONE_2,
        Constant.ID_BUILDING_STONE_3,
        Constant.ID_BUILDING_STONE_4,
    ),
}


def _occupied_map_tiles(town):
    """Tiles occupied by persisted item footprints."""
    occupied = set()
    for item in town.get("items", []):
        if not item or len(item) < 3:
            continue
        try:
            item_id, x, y = int(item[0]), int(item[1]), int(item[2])
            width = max(
                1, int(get_attribute_from_item_id(item_id, "width") or 1)
            )
            height = max(
                1, int(get_attribute_from_item_id(item_id, "height") or 1)
            )
            orientation = int(item[3] or 0) if len(item) > 3 else 0
        except (TypeError, ValueError):
            continue
        if orientation % 2:
            width, height = height, width
        for tx in range(x, x + width):
            for ty in range(y, y + height):
                if 0 <= tx < 100 and 0 <= ty < 100:
                    occupied.add((tx, ty))
    return occupied


def _big_tile_for_position(position):
    x, y = position
    if not (0 <= int(x) < 100 and 0 <= int(y) < 100):
        return None
    return (int(y) // 20) * 5 + (int(x) // 20) + 1


def _position_is_wild(town, position):
    """Natural resources may remain on newly bought land until harvested.

    New/regrown resources, however, belong outside every owned expansion. The
    centre tile (13) is owned even in malformed legacy saves which omitted the
    expansions list.
    """
    try:
        owned = {int(value) for value in town.get("expansions", [])}
    except (TypeError, ValueError):
        owned = set()
    owned.add(13)
    big_tile = _big_tile_for_position(position)
    return big_tile is not None and big_tile not in owned


def _town_has_operational_market(town):
    """Whether this town contains a built Market the player can actually use."""
    for item in town.get("items", []):
        if not item:
            continue
        try:
            functional = int(get_attribute_from_item_id(
                int(item[0]), "subcat_functional"
            ))
        except (TypeError, ValueError, IndexError):
            continue
        if functional != Constant.SUBCATFUNC_BUILDING_MARKET:
            continue
        attrs = item[7] if len(item) > 7 and isinstance(item[7], dict) else {}
        # Social Market tiers are normalized before this check. Their `si`
        # field is a list while roles remain vacant and None after opening.
        # Non-social upgrade tiers have no `si` key and are operational once
        # their construction has completed.
        if "si" not in attrs or attrs.get("si") is None:
            return True
    return False


def _item_subcat(item):
    try:
        return int(get_attribute_from_item_id(
            int(item[0]), "subcat_functional"
        ))
    except (TypeError, ValueError, IndexError):
        return -1


def _count_items(towns, *, ids=None, subcat=None):
    wanted = {int(value) for value in ids} if ids is not None else None
    total = 0
    for town in towns:
        for item in town.get("items", []):
            if not item:
                continue
            if wanted is not None and int(item[0]) not in wanted:
                continue
            if subcat is not None and _item_subcat(item) != int(subcat):
                continue
            total += 1
    return total


def _count_contained(towns, building_ids, unit_subcat):
    wanted = {int(value) for value in building_ids}
    total = 0
    for town in towns:
        for item in town.get("items", []):
            if not item or int(item[0]) not in wanted:
                continue
            contained = item[6] if len(item) > 6 and isinstance(item[6], list) else []
            for raw_unit in contained:
                unit_id = raw_unit[0] if isinstance(raw_unit, list) and raw_unit else raw_unit
                try:
                    if int(get_attribute_from_item_id(
                        int(unit_id), "subcat_functional"
                    )) == int(unit_subcat):
                        total += 1
                except (TypeError, ValueError, IndexError):
                    continue
    return total


def _repair_persisted_mission_progress(save):
    """Recover state-based goals whose one-shot client event was missed.

    Building/containment goals describe the village's current state, so they
    remain true after a reload or an upgrade. Action/history goals (collect,
    kill, trade, attack, etc.) are intentionally not inferred here.
    """
    pstate = save.setdefault("privateState", {})
    completed = pstate.setdefault("completedMissions", [])
    completed_ids = {
        _safe_int(value, -1) for value in completed
    }
    towns = save.get("maps", [])
    barracks = {
        Constant.ID_BUILDING_BARRACKS_1,
        Constant.ID_BUILDING_BARRACKS_2,
        Constant.ID_BUILDING_BARRACKS_3,
    }
    archery = {
        Constant.ID_BUILDING_ARCHERY_1,
        Constant.ID_BUILDING_ARCHERY_2,
        Constant.ID_BUILDING_ARCHERY_3,
    }
    sheep_ranches = {
        Constant.ID_BUILDING_RANCH_SHEEP,
        Constant.ID_BUILDING_RANCH_SHEEP_2,
        Constant.ID_BUILDING_RANCH_SHEEP_3,
    }
    cow_ranches = {
        Constant.ID_BUILDING_RANCH_COW,
        Constant.ID_BUILDING_RANCH_COW_2,
        Constant.ID_BUILDING_RANCH_COW_3,
    }
    gold_mines = {
        Constant.ID_BUILDING_MINE_GOLD_1,
        Constant.ID_BUILDING_MINE_GOLD_2,
        Constant.ID_BUILDING_MINE_GOLD_3,
    }
    lumber_mills = {
        Constant.ID_BUILDING_MINE_WOOD_1,
        Constant.ID_BUILDING_MINE_WOOD_2,
        Constant.ID_BUILDING_MINE_WOOD_3,
        Constant.ID_BUILDING_MINE_WOOD_4,
    }
    observable = {
        1: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_FARM) >= 1,
        2: _count_items(towns, subcat=Constant.SUBCATFUNC_UNIT_PEASANT) >= 2,
        7: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_FARM) >= 2,
        8: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_HOUSE) >= 4,
        9: _count_items(towns, ids=barracks) >= 1,
        11: _count_items(towns, subcat=Constant.SUBCATFUNC_UNIT_PEASANT) >= 4,
        12: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_MILL) >= 1,
        13: _count_contained(
            towns,
            {
                Constant.ID_BUILDING_WINDMILL_1,
                Constant.ID_BUILDING_WINDMILL_2,
                Constant.ID_BUILDING_WINDMILL_3,
                Constant.ID_BUILDING_WINDMILL_4,
            },
            Constant.SUBCATFUNC_UNIT_PEASANT,
        ) >= 1,
        15: _count_contained(
            towns,
            {
                Constant.ID_BUILDING_WINDMILL_1,
                Constant.ID_BUILDING_WINDMILL_2,
                Constant.ID_BUILDING_WINDMILL_3,
                Constant.ID_BUILDING_WINDMILL_4,
            },
            Constant.SUBCATFUNC_UNIT_PEASANT,
        ) >= 2,
        20: _count_items(towns, ids=sheep_ranches) >= 1,
        22: _count_contained(
            towns, sheep_ranches, Constant.SUBCATFUNC_UNIT_SHEEP
        ) >= 1,
        24: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_FARM) >= 5,
        26: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_HOUSE) >= 8,
        27: _count_items(towns, ids=gold_mines) >= 1,
        28: _count_contained(
            towns, gold_mines, Constant.SUBCATFUNC_UNIT_PEASANT
        ) >= 1,
        29: _count_items(towns, ids=archery) >= 1,
        32: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_DECO) >= 5,
        36: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_TOWER) >= 3,
        37: _count_items(towns, ids={Constant.ID_BUILDING_WALL_1}) >= 20,
        39: _count_items(towns, subcat=Constant.SUBCATFUNC_BUILDING_STABLE) >= 1,
        45: _count_contained(
            towns, lumber_mills, Constant.SUBCATFUNC_UNIT_PEASANT
        ) >= 1,
        47: _count_contained(
            towns, lumber_mills, Constant.SUBCATFUNC_UNIT_PEASANT
        ) >= 2,
        48: _count_items(towns, ids=cow_ranches) >= 1,
        49: _count_contained(
            towns, cow_ranches, Constant.SUBCATFUNC_UNIT_COW
        ) >= 1,
        50: _count_items(
            towns, subcat=Constant.SUBCATFUNC_BUILDING_MARKET
        ) >= 1,
        # Open Market is independent of the earlier "spend cash" goal. The
        # client can miss its one-shot open event, especially after upgrading.
        53: any(_town_has_operational_market(town) for town in towns),
        58: _count_items(
            towns, ids={Constant.ID_BUILDING_MINE_STONE_1}
        ) >= 1,
        69: _count_items(towns, ids={Constant.ID_BUILDING_HOUSE_2}) >= 1,
    }
    changed = False
    for mission_id, fulfilled in observable.items():
        if fulfilled and mission_id not in completed_ids:
            completed.append(mission_id)
            completed_ids.add(mission_id)
            changed = True
    return changed


def _random_wild_resource_position(town, occupied, excluded):
    """Choose an empty tile from the same wild regions as MapInitializer.

    The stock map is a 5x5 grid of 20x20 big tiles. Natural deposits are
    placed on the permanent outer border plus inner tiles the player has not
    bought. Excluding ``town.expansions`` reproduces that rule and prevents a
    respawn from occupying owned/buildable land.
    """
    try:
        owned = {int(value) for value in town.get("expansions", [])}
    except (TypeError, ValueError):
        owned = set()
    owned.add(13)
    big_tiles = [value for value in range(1, 26) if value not in owned]
    if not big_tiles:
        return None

    def candidate(big_tile, local_x, local_y):
        column = (big_tile - 1) % 5
        row = (big_tile - 1) // 5
        return column * 20 + local_x, row * 20 + local_y

    # Random first, matching the original spawn style. The deterministic
    # fallback guarantees that an unusually crowded map does not make a
    # matured timer disappear merely because random probing missed a gap.
    for _ in range(512):
        position = candidate(
            random.choice(big_tiles),
            random.randrange(20),
            random.randrange(20),
        )
        if position not in occupied and position != excluded:
            return position
    for big_tile in big_tiles:
        for local_y in range(20):
            for local_x in range(20):
                position = candidate(big_tile, local_x, local_y)
                if position not in occupied and position != excluded:
                    return position
    return None


def _wild_cluster_positions(town, occupied, count):
    """Return up to ``count`` nearby empty wild tiles.

    MapInitializer represents one visible deposit as a cluster of individual
    ten-resource nodes. Keep that original representation while repairing old
    under-populated saves; do not scatter every repaired node in isolation.
    """
    if count <= 0:
        return []
    anchor = _random_wild_resource_position(town, occupied, None)
    if anchor is None:
        return []
    candidates = []
    for radius in range(0, 7):
        ring = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue
                ring.append((anchor[0] + dx, anchor[1] + dy))
        random.shuffle(ring)
        candidates.extend(ring)
    result = []
    for position in candidates:
        if (
            position not in occupied
            and _position_is_wild(town, position)
        ):
            result.append(position)
            occupied.add(position)
            if len(result) >= count:
                break
    while len(result) < count:
        position = _random_wild_resource_position(town, occupied, None)
        if position is None:
            break
        result.append(position)
        occupied.add(position)
    return result


def _nearby_wild_cluster_positions(town, occupied, anchor, count):
    """Fill empty wild tiles around an existing resource-cluster anchor."""
    result = []
    for radius in range(1, 7):
        ring = [
            (anchor[0] + dx, anchor[1] + dy)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if max(abs(dx), abs(dy)) == radius
        ]
        random.shuffle(ring)
        for position in ring:
            if position in occupied or not _position_is_wild(town, position):
                continue
            result.append(position)
            occupied.add(position)
            if len(result) >= count:
                return result
    return result


def _clustered_respawn_positions(
    town, occupied, entries, visible_positions, maximum_cluster=6
):
    """Choose persisted respawn tiles without scattering individual nodes.

    Source tiles within three cells form one depleted deposit. A partially
    harvested deposit grows beside its remaining nodes. If the whole deposit
    is gone, all timers which mature together move to one new wild cluster.
    This keeps mineral deposits at the stock 4-6-node shape and makes trees
    return as forests instead of isolated dots.
    """
    if not entries:
        return []
    sources = [
        (int(entry["source_x"]), int(entry["source_y"]))
        for entry in entries
    ]
    ungrouped = set(range(len(entries)))
    groups = []
    while ungrouped:
        seed = ungrouped.pop()
        group = {seed}
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            near = {
                index for index in ungrouped
                if max(
                    abs(sources[index][0] - sources[current][0]),
                    abs(sources[index][1] - sources[current][1]),
                ) <= 3
            }
            ungrouped -= near
            group |= near
            frontier.extend(near)
        groups.append(sorted(group))

    planned = [None] * len(entries)
    existing = list(visible_positions)
    for group in groups:
        for start in range(0, len(group), maximum_cluster):
            chunk = group[start:start + maximum_cluster]
            nearby = [
                position for position in existing
                if any(
                    max(
                        abs(position[0] - sources[index][0]),
                        abs(position[1] - sources[index][1]),
                    ) <= 4
                    for index in chunk
                )
            ]
            if nearby:
                positions = _nearby_wild_cluster_positions(
                    town, occupied, random.choice(nearby), len(chunk)
                )
            else:
                positions = _wild_cluster_positions(
                    town, occupied, len(chunk)
                )
            for index, position in zip(chunk, positions):
                planned[index] = position
                existing.append(position)
    return planned


# Stock MapInitializer creates three deposits of 4-6 nodes. The server owns
# the durable population, with 21 retained as the legacy absolute maximum.
_MINERAL_FAMILY_CAP = 21
_NATURAL_RESOURCE_POPULATION_REPAIR_VERSION = 3


def _cluster_sizes(count):
    """Partition a population into 4-6-node groups where possible."""
    count = max(0, int(count))
    if count == 0:
        return []
    for parts in range((count + 5) // 6, count // 4 + 1):
        base, extra = divmod(count, parts)
        if 4 <= base <= 6 and base + (1 if extra else 0) <= 6:
            return [base + 1] * extra + [base] * (parts - extra)
    # One to three nodes can be the visible remainder of a deposit whose
    # siblings are still on cooldown; future regrowth joins this remainder.
    return [count]


def _recluster_visible_resources(town, families):
    """One-time migration from old independently scattered spawn layouts."""
    changed = False
    for ids, _minimum in families.values():
        resources = [
            item for item in town.get("items", [])
            if item and int(item[0]) in ids
        ]
        if not resources:
            continue
        town["items"] = [
            item for item in town.get("items", [])
            if not item or int(item[0]) not in ids
        ]
        occupied = _occupied_map_tiles(town)
        placed = 0
        for size in _cluster_sizes(len(resources)):
            positions = _wild_cluster_positions(town, occupied, size)
            for item, position in zip(
                resources[placed:placed + size], positions
            ):
                if len(item) >= 3:
                    if [int(item[1]), int(item[2])] != [position[0], position[1]]:
                        changed = True
                    item[1], item[2] = position
                town["items"].append(item)
                placed += 1
            if len(positions) < size:
                break
        # A completely full wild map must not lose anything that could not be
        # placed. Preserve those entries at their previous persisted tiles.
        town["items"].extend(resources[placed:])
    return changed


def _repair_natural_resource_population(save, now):
    """Repair saves depleted by older non-persistent respawns.

    Stock maps start with 160-300 trees and three 4-6-node clusters for each
    mineral, with 21 retained as the legacy ceiling. Count pending cooldowns
    as population so an ordinary harvest never receives a free replacement.
    The old scattered layout and population are migrated once per repair
    version. Version 3 specifically heals deployed version-2 saves whose
    marker was persisted even though one mineral family was absent. Counting
    pending timers prevents that migration from bypassing legitimate harvest
    cooldowns.
    """
    if _tutorial_is_incomplete(save):
        return False
    changed = False
    families = {
        "tree": (
            {
                Constant.ID_BUILDING_TREE_1,
                Constant.ID_BUILDING_TREE_2,
                Constant.ID_BUILDING_TREE_3,
            },
            160,
        ),
        "gold": (set(_MINERAL_RESPAWN_IDS["gold"]), 15),
        "stone": (set(_MINERAL_RESPAWN_IDS["stone"]), 15),
    }
    for town in save.get("maps", []):
        try:
            version = int(
                town.get("naturalResourcePopulationRepairVersion", 0) or 0
            )
        except (TypeError, ValueError):
            version = 0
        if version >= _NATURAL_RESOURCE_POPULATION_REPAIR_VERSION:
            continue
        if _recluster_visible_resources(town, families):
            changed = True
        items = town.setdefault("items", [])
        occupied = _occupied_map_tiles(town)
        pending_trees = town.get("pendingTreeRespawns", [])
        pending_minerals = town.get("pendingMineralRespawns", [])
        for family, (ids, minimum) in families.items():
            visible = sum(
                1 for item in items if item and int(item[0]) in ids
            )
            if family == "tree":
                pending = sum(
                    1 for entry in pending_trees
                    if isinstance(entry, dict)
                    and _safe_int(entry.get("id", -1), -1) in ids
                )
            else:
                pending = sum(
                    1 for entry in pending_minerals
                    if isinstance(entry, dict)
                    and str(entry.get("family")) == family
                )
            missing = max(0, minimum - visible - pending)
            for cluster_size in _cluster_sizes(missing):
                # Original mineral clusters contain 4-6 nodes. Trees use
                # larger clusters, but six keeps the repair compact without
                # producing an unnatural solid block.
                positions = _wild_cluster_positions(
                    town, occupied, cluster_size
                )
                if not positions:
                    break
                family_ids = tuple(sorted(ids))
                for index, position in enumerate(positions):
                    resource_id = family_ids[
                        (visible + index) % len(family_ids)
                    ]
                    items.append([
                        resource_id, position[0], position[1],
                        0, int(now), 0,
                    ])
                visible += len(positions)
                changed = True
        town["naturalResourcesInitialized"] = 1
        town["naturalResourcePopulationRepairVersion"] = (
            _NATURAL_RESOURCE_POPULATION_REPAIR_VERSION
        )
        changed = True
    return changed


def _respawn_mature_minerals(save, now):
    """Restore depleted gold/stone without rerolling them on reload.

    Each harvested node owns one persisted three-hour timer carrying the tile
    it was mined from. Once mature, its replacement receives one new random
    empty wild tile, matching tree regrowth. The chosen node is then persisted:
    a reload by itself never relocates it or advances the timer.
    """
    changed = False
    for town in save.get("maps", []):
        pending = town.get("pendingMineralRespawns")
        if not isinstance(pending, list) or not pending:
            continue
        items = town.setdefault("items", [])
        occupied_tiles = _occupied_map_tiles(town)
        family_counts = {
            family: sum(
                1 for item in items
                if item and int(item[0]) in family_ids
            )
            for family, family_ids in _MINERAL_RESPAWN_IDS.items()
        }
        remaining = []
        ready_by_family = {family: [] for family in _MINERAL_RESPAWN_IDS}
        for entry in pending:
            try:
                family = str(entry["family"])
                x, y = int(entry["source_x"]), int(entry["source_y"])
                ready_at = int(entry["at"])
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if family not in _MINERAL_RESPAWN_IDS:
                changed = True
                continue
            if int(now) < ready_at:
                remaining.append(entry)
                continue
            ready_by_family[family].append({
                "entry": entry,
                "source_x": x,
                "source_y": y,
            })
        for family, ready in ready_by_family.items():
            available = max(0, _MINERAL_FAMILY_CAP - family_counts[family])
            spawnable, overflow = ready[:available], ready[available:]
            # Grandfather a legacy over-cap map without deleting nodes. Its
            # population naturally converges to 21 as those extras are mined;
            # their excess timers remain pending rather than multiplying it.
            remaining.extend(value["entry"] for value in overflow)
            visible_positions = [
                (int(item[1]), int(item[2]))
                for item in items
                if item and int(item[0]) in _MINERAL_RESPAWN_IDS[family]
                and len(item) >= 3
            ]
            positions = _clustered_respawn_positions(
                town, occupied_tiles, spawnable, visible_positions
            )
            for value, position in zip(spawnable, positions):
                if position is None:
                    remaining.append(value["entry"])
                    continue
                entry = value["entry"]
                try:
                    resource_id = int(entry.get("id", 0)) or 0
                except (TypeError, ValueError):
                    resource_id = 0
                if resource_id not in _MINERAL_RESPAWN_IDS[family]:
                    resource_id = _MINERAL_RESPAWN_IDS[family][0]
                items.append([
                    resource_id, position[0], position[1],
                    0, int(now), 0,
                ])
                family_counts[family] += 1
                changed = True
        if remaining != pending:
            town["pendingMineralRespawns"] = remaining
    return changed


_ALL_NATURAL_RESOURCE_IDS = (
    frozenset(_MINERAL_RESPAWN_IDS["gold"])
    | frozenset(_MINERAL_RESPAWN_IDS["stone"])
    | {Constant.ID_BUILDING_TREE_1, Constant.ID_BUILDING_TREE_2,
       Constant.ID_BUILDING_TREE_3}
)


def _town_has_natural_resources(town):
    """True if the town still has wild trees/minerals present or a pending
    server-side respawn scheduled for one. Used to re-lock the reload marker
    after the one-time legacy repopulation so it cannot re-fire each reload."""
    for item in town.get("items", []):
        if item and int(item[0]) in _ALL_NATURAL_RESOURCE_IDS:
            return True
    for key in ("pendingTreeRespawns", "pendingMineralRespawns"):
        pending = town.get(key)
        if isinstance(pending, list) and pending:
            return True
    return False


def _sync_natural_resource_reload_marker(save, map_idx):
    """Prevent MapInitializer from repopulating resources on every reload.

    The patched client checks arrayAnimals[SUBCATFUNC_RESOURCE_REGEN] around
    only its tree/mineral population blocks. Animals keep their independent
    daily allowances. A new town is left unmarked until its first environment
    batch is persisted; established towns use server-authoritative pending
    tree/mineral cooldowns.
    """
    animals = save["privateState"].setdefault("arrayAnimals", {})
    marker = str(Constant.SUBCATFUNC_RESOURCE_REGEN)
    town = save["maps"][map_idx]
    tutorial_done = int(
        save["playerInfo"].get("completed_tutorial", 0) or 0
    )
    initialized = int(town.get("naturalResourcesInitialized", 0) or 0)
    # A legacy-empty town is reopened once (naturalResourcesInitialized=0) so
    # MapInitializer can repopulate it. Re-lock the flag the moment the town
    # actually has its wild resources again (present items or a pending
    # respawn), otherwise the flag stays 0, the marker is cleared on EVERY
    # reload, and the client repopulation pass re-randomizes - i.e. visibly
    # "wanders" - the existing trees/minerals each time.
    if tutorial_done and not initialized and _town_has_natural_resources(town):
        town["naturalResourcesInitialized"] = 1
        initialized = 1
    if tutorial_done and initialized:
        animals[marker] = 1
    else:
        animals.pop(marker, None)


def _clamp_map(save, map_number):
    """A valid map index for this village. Switching towns (or clicking
    "Town" with one town) requests map 1; an out-of-range index would 500
    and, because the client's map-load error handler is a no-op, freeze the
    game on the loading bar. Fall back to the default map."""
    maps = save["maps"]
    default = int(save["playerInfo"].get("default_map", 0) or 0)
    if map_number is None or map_number < 0 or map_number >= len(maps):
        map_number = default if 0 <= default < len(maps) else 0
    return map_number

def get_player_info(USERID, map_number=None):
    # Update last logged in
    ts_now = timestamp_now()
    save = session(USERID)
    # Server startup repairs legacy saves, but a building can also be placed
    # while this process is already running.  The Flash client treats a
    # missing ``attrs.si`` differently from an explicit empty list: missing
    # means open, while [] means "still needs every worker".  Normalize on
    # every player load so a browser refresh can never bypass an entirely
    # unfilled Market, Stone Mine, or other staffed building.
    tutorial_progress_changed = _repair_tutorial_progress(save)
    tutorial_targets_changed = _repair_tutorial_targets(save)
    camp_state_changed = _repair_active_enemy_camps(save)
    social_state_changed = _repair_social_building_state(save)
    natural_state_changed = _repair_natural_resource_state(save)
    animal_budget_changed = _refresh_daily_animal_budget(save, ts_now)
    if _tutorial_is_incomplete(save):
        trees_changed = False
        minerals_changed = False
        natural_population_changed = False
    else:
        trees_changed = _respawn_mature_trees(save, ts_now)
        minerals_changed = _respawn_mature_minerals(save, ts_now)
        natural_population_changed = _repair_natural_resource_population(
            save, ts_now
        )
    mission_progress_changed = _repair_persisted_mission_progress(save)
    save["playerInfo"]["last_logged_in"] = ts_now
    # dartsHasFree means "free game claimed (darts_new_free) but not yet
    # thrown". The client reads it at login and, on a new local day, claims a
    # fresh free game itself - so don't recompute it here, just repair stale
    # saves where a throw happened after the claim but the flag was never
    # consumed (pre-fix saves), so a reload can't hand out a bonus free throw.
    pState = save["privateState"]
    if "timeStampLastDart" not in pState:
        pState["timeStampLastDart"] = int(pState.get("timeStampDartsNewFree", 0) or 0)
    last_claim = int(pState.get("timeStampDartsNewFree", 0) or 0)
    last_dart = int(pState.get("timeStampLastDart", 0) or 0)
    if pState.get("dartsHasFree") and last_claim > 0 and last_dart >= last_claim:
        pState["dartsHasFree"] = False
    # Supreme Bahamut Temple: ensure the fields the client reads on load exist,
    # so older saves get valid Temple state automatically (the client does
    # Utils.inArray(step, privateState["templeStep"]) and would break on an
    # undefined list). templeStep = completed step indices; timeStampTemple =
    # last-step time that gates the 48h wait.
    pState.setdefault("templeStep", [])
    pState.setdefault("timeStampTemple", 0)
    # PvP client code iterates these lists and performs arithmetic on the
    # counters during every load; missing/null legacy values break histories,
    # cooldowns and goal progress.
    if not isinstance(pState.get("attacksSent"), list):
        pState["attacksSent"] = []
    if not isinstance(pState.get("attacksReceived"), list):
        pState["attacksReceived"] = []
    for pvp_key in (
        "attacksWon", "attacksLost", "honor", "tsAttacksReset",
        "attacksPack", "spyingsPack", "tsSpyingsReset",
    ):
        try:
            pState[pvp_key] = int(pState.get(pvp_key, 0) or 0)
        except (TypeError, ValueError):
            pState[pvp_key] = 0
    if not isinstance(pState.get("spyings"), list):
        pState["spyings"] = []
    # The local server has no separate Facebook display name, so the editable
    # default-town name is the player's identity. Keep it synchronized after
    # every rename; otherwise cards stay stuck on "Emperor" or an older name.
    pi = save["playerInfo"]
    names = pi.get("map_names") or []
    dm = int(pi.get("default_map", 0) or 0)
    if 0 <= dm < len(names) and names[dm]:
        pi["name"] = names[dm]
    # Market state is a 20-hour period in the Flash client.
    for m in save["maps"]:
        try:
            trades_done = int(m.get("numTradesDone", 0) or 0)
        except (TypeError, ValueError):
            trades_done = 0
        m["numTradesDone"] = min(max(trades_done, 0), 20)
        try:
            m["timestampLastTrade"] = max(
                0, int(m.get("timestampLastTrade", 0) or 0)
            )
        except (TypeError, ValueError):
            m["timestampLastTrade"] = 0
        if not isinstance(m.get("resourcesTraded"), dict):
            m["resourcesTraded"] = {}
        m.setdefault("resourceAlliesMarket", "n")
        # The original protocol separates owned-item storage (map.store) from
        # received gifts (privateState.gifts).  Keep this as an object even for
        # starter saves that contain null, otherwise a stored building reloads
        # as a gift and incorrectly awards its construction XP when re-placed.
        raw_store = m.get("store")
        normalized_store = {}
        if isinstance(raw_store, dict):
            store_values = raw_store.items()
        elif isinstance(raw_store, list):
            store_values = enumerate(raw_store)
        else:
            store_values = ()
        for raw_id, raw_count in store_values:
            try:
                item_id, count = int(raw_id), int(raw_count)
            except (TypeError, ValueError):
                continue
            if item_id >= 0 and count > 0:
                normalized_store[str(item_id)] = count
        m["store"] = normalized_store
        # Unit Warehouse state is per-town. Older/new saves omitted these
        # fields, which made the client show zero slots and forget stored units
        # after refresh.
        m.setdefault("warehouseAditionalCapacitySingle", 0)
        m.setdefault("warehousedUnits", {})
        if (
            int(m.get("warehouseAditionalCapacitySingle", 0) or 0) < 1
            and any(
                item
                and int(item[0]) == Constant.ID_BUILDING_UNIT_WAREHOUSE
                for item in m.get("items", [])
            )
        ):
            # The building includes its first slot. The 2-cash action buys
            # one *additional* slot, rather than activating a 20-cash shell.
            m["warehouseAditionalCapacitySingle"] = 1
        last_trade = int(m.get("timestampLastTrade", 0) or 0)
        if last_trade and ts_now - last_trade >= 20 * 3600:
            m["numTradesDone"] = 0
            m["resourcesTraded"] = {}
            m["timestampLastTrade"] = 0
    # Item collectibles: the client loads privateState.collections (per
    # collection: [completedFlag, count, count, ...]) and collectionsCompleted.
    # Without them the client's load silently fails (try/catch) and collected
    # items vanish on reload. Seed the structures (NUM_COLLECTIONS = 23, so 24
    # slots) so add_collectable has somewhere to persist.
    pState.setdefault("collectionsCompleted", [])
    colls = pState.setdefault("collections", [])
    while len(colls) < 24:
        colls.append([0])
    # Collection index 0 is a 1-based-alignment dummy (the client's arCollected
    # and collectible ids are 1..NUM_COLLECTIONS). The client's load loop starts
    # at i=0 and, for any non-empty collections[0], does arCollected[0][0]=... -
    # but arCollected[0] never exists, so it throws; the surrounding try/catch
    # then aborts the WHOLE loop and every earned collectible vanishes on
    # reload. An empty entry at index 0 makes the loader skip it.
    colls[0] = []
    _ensure_town_list(save)
    _sync_global_level(save)
    # player
    map_idx = _clamp_map(save, map_number)
    _sync_natural_resource_reload_marker(save, map_idx)
    camp_timer_changed = refresh_enemy_camp_timer(
        save["maps"][map_idx], ts_now
    )
    response_pstate = copy.deepcopy(pState)
    # The old Flash JSON reader treats a bare false as a truthy object. Keep
    # Python/save state boolean, but emit this protocol flag as numeric 0/1.
    response_pstate["dartsHasFree"] = 1 if pState.get("dartsHasFree") else 0
    player_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": ts_now,
        "playerInfo": save["playerInfo"],
        "map": save["maps"][map_idx],
        "privateState": response_pstate,
        "neighbors": neighbors(USERID)
    }
    if (
        tutorial_progress_changed
        or tutorial_targets_changed
        or camp_state_changed
        or trees_changed
        or minerals_changed
        or social_state_changed
        or natural_state_changed
        or natural_population_changed
        or animal_budget_changed
        or mission_progress_changed
        or camp_timer_changed
    ):
        save_session(USERID)
    return player_info

def _seed_neighbor_client_state(save, now):
    """Seed the privateState / per-map structures the client reads when it
    loads a VISITED village. The self-load path seeds these inline in
    get_player_info, but a bundled neighbor save such as Arthur omits them
    (collections, templeStep, PvP lists, market/warehouse fields); the client's
    loader then silently fails in a try/catch and, because its map-load error
    handler is a no-op, the loading bar loops 0->100 forever. Idempotent."""
    pState = save["privateState"]
    pState.setdefault("timeStampLastDart",
                      int(pState.get("timeStampDartsNewFree", 0) or 0))
    pState.setdefault("templeStep", [])
    pState.setdefault("timeStampTemple", 0)
    if not isinstance(pState.get("attacksSent"), list):
        pState["attacksSent"] = []
    if not isinstance(pState.get("attacksReceived"), list):
        pState["attacksReceived"] = []
    for pvp_key in (
        "attacksWon", "attacksLost", "honor", "tsAttacksReset",
        "attacksPack", "spyingsPack", "tsSpyingsReset",
    ):
        try:
            pState[pvp_key] = int(pState.get(pvp_key, 0) or 0)
        except (TypeError, ValueError):
            pState[pvp_key] = 0
    if not isinstance(pState.get("spyings"), list):
        pState["spyings"] = []
    pState.setdefault("collectionsCompleted", [])
    colls = pState.setdefault("collections", [])
    while len(colls) < 24:
        colls.append([0])
    colls[0] = []
    for m in save["maps"]:
        try:
            trades_done = int(m.get("numTradesDone", 0) or 0)
        except (TypeError, ValueError):
            trades_done = 0
        m["numTradesDone"] = min(max(trades_done, 0), 20)
        try:
            m["timestampLastTrade"] = max(
                0, int(m.get("timestampLastTrade", 0) or 0)
            )
        except (TypeError, ValueError):
            m["timestampLastTrade"] = 0
        if not isinstance(m.get("resourcesTraded"), dict):
            m["resourcesTraded"] = {}
        m.setdefault("resourceAlliesMarket", "n")
        m.setdefault("warehouseAditionalCapacitySingle", 0)
        m.setdefault("warehousedUnits", {})


def _is_pvp_camp_entity(item):
    """True if a saved item is part of a human village's invading enemy camp.

    The camp is entirely troll-RACE (config race 't': goblins/trolls of every
    tier - TROLL_1..8, Rhinorider, Healer, ... - plus troll camp buildings),
    or a neutral camp prize marker (prisoners/treasures/totems, in
    _ENEMY_CAMP_MARKER_IDS). Player-ownable "trolls" (rehabbed/good/war) are
    config race 'h', so they are NOT matched. Using the config race instead of
    a hand-maintained id list means new/higher-tier camp units are covered
    automatically."""
    if not item:
        return False
    iid = int(item[0])
    if iid in _ENEMY_CAMP_MARKER_IDS:
        return True
    return str(get_attribute_from_item_id(iid, "race")) == "t"


def _strip_pvp_camp(town):
    """Return the town with its invading enemy camp removed (human towns only).

    A PvP attacker/visitor loads the defender's map; the neutral goblin/troll
    camp otherwise spawns and attacks them. Deep-copy so the defender's real
    save keeps its camp - only the served copy is stripped. Troll-race towns
    are left untouched (their own base legitimately uses troll-race ids)."""
    if not isinstance(town, dict) or str(town.get("race", "h")) != "h":
        return town
    items = town.get("items") or []
    tracked = {
        (int(entry[0]), int(entry[1]), int(entry[2]))
        for entry in town.get("enemyCampRoster", [])
        if isinstance(entry, list) and len(entry) >= 3
    }

    def belongs_to_camp(item):
        if not item:
            return False
        key = (int(item[0]), int(item[1]), int(item[2]))
        if tracked:
            return key in tracked or int(item[0]) in _ENEMY_CAMP_MARKER_IDS
        # Legacy saves predate the exact roster. Use the old race-based
        # fallback only for them; once a roster exists, an untracked troll can
        # be a legitimate defending unit and must remain in the PvP village.
        return _is_pvp_camp_entity(item)

    if not any(belongs_to_camp(item) for item in items):
        return town
    clone = copy.deepcopy(town)
    clone["items"] = [
        item for item in clone.get("items", [])
        if not belongs_to_camp(item)
    ]
    clone["enemyCampActive"] = 0
    clone["enemyCampRoster"] = []
    return clone


def get_neighbor_info(userid, map_number):
    save = neighbor_session(userid)
    if save is None:
        return ({"result": "error", "error": "unknown_user"}, 404)
    # Visiting a local save must show the same editable empire name and global
    # level as its owner sees. Previously only the self-load path repaired
    # these fields, leaving visit/player cards stuck on the legacy "Emperor".
    pi = save["playerInfo"]
    names = pi.get("map_names") or []
    default = int(pi.get("default_map", 0) or 0)
    if 0 <= default < len(names) and names[default]:
        pi["name"] = names[default]
    _ensure_town_list(save)
    _sync_global_level(save)
    now = timestamp_now()
    _seed_neighbor_client_state(save, now)
    map_idx = _clamp_map(save, map_number)
    response_pstate = copy.deepcopy(save["privateState"])
    # The old Flash JSON reader treats a bare false as a truthy object; emit
    # this protocol flag as numeric 0/1 (matches the self-load path).
    response_pstate["dartsHasFree"] = 1 if save["privateState"].get("dartsHasFree") else 0
    neighbor_info = {
        "result": "ok",
        "processed_errors": 0,
        "timestamp": now,
        "playerInfo": save["playerInfo"],
        "map": _strip_pvp_camp(save["maps"][map_idx]),
        "privateState": response_pstate,
        "neighbors": neighbors(userid)
    }
    return neighbor_info


def get_public_player_info(userid):
    """Public PvP profile served at get_public_player_info.php.

    PopupPlayerProfile reads these exact keys: name, level, map_names[0]
    (empire), honor_points, country, last_logged_in (unix seconds),
    attacks_won, attacks_lost and pid. The PvP counters live in the target
    player's privateState and are updated by end_attack for both attacker and
    defender, so this profile reflects the latest battle results instead of the
    old empty stub."""
    save = neighbor_session(str(userid))
    if save is None:
        return ({"result": "error", "error": "unknown_user"}, 404)
    pi = save.get("playerInfo", {}) or {}
    ps = save.get("privateState", {}) or {}
    maps = save.get("maps") or [{}]
    default = int(pi.get("default_map", 0) or 0)
    if default < 0 or default >= len(maps):
        default = 0
    town = maps[default]

    def _int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    map_names = pi.get("map_names")
    if not isinstance(map_names, list) or not map_names:
        map_names = [_display_name(save)]
    return {
        "result": "ok",
        "pid": str(userid),
        "name": _display_name(save),
        "level": max(1, int(get_level_from_xp(_int(town.get("xp", 0))))),
        "map_names": map_names,
        "honor_points": _int(ps.get("honor")),
        "attacks_won": _int(ps.get("attacksWon")),
        "attacks_lost": _int(ps.get("attacksLost")),
        "country": str(pi.get("country") or ""),
        "last_logged_in": _int(
            pi.get("last_logged_in") or ps.get("lastLogin") or timestamp_now()
        ),
    }
