import time

from get_game_config import get_attribute_from_item_id

def apply_cost(playerInfo: dict, map: dict, id: int, price_multiplier: int) -> None:
    cost = int(price_multiplier * int(get_attribute_from_item_id(id, "cost")))
    cost_type = get_attribute_from_item_id(id, "cost_type")
    if cost_type == "w":
        map["wood"] = max(map["wood"] - cost, 0)
    elif cost_type == "g":
        map["coins"] = max(map["coins"] - cost, 0)
    elif cost_type == "c":
        playerInfo["cash"] = max(playerInfo["cash"] - cost, 0)
    elif cost_type == "s":
        map["stone"] = max(map["stone"] - cost, 0)
    elif cost_type == "f":
        map["food"] = max(map["food"] - cost, 0)

def apply_collect(
    playerInfo: dict,
    map: dict,
    id: int,
    resource_multiplier: int = 1,
    worker_count: float = 1,
    production_multiplier: float = 1,
) -> None:
    """Persist the same resource yield the Flash client displays.

    The client adds 20% of the base yield for every contained worker after
    the first, then applies the selected mine/mill duration multiplier.
    Keeping the calculation here prevents those visible bonuses disappearing
    on the next server synchronization.
    """
    base_collect = int(
        resource_multiplier * int(get_attribute_from_item_id(id, "collect"))
    )
    worker_count = max(1.0, float(worker_count or 1))
    collect = int(
        (
            base_collect
            + (worker_count - 1) * base_collect * 0.2
        )
        * float(production_multiplier)
    )
    collect_type = get_attribute_from_item_id(id, "collect_type")
    apply_collect_xp(map, id)
    if collect_type == "w":
        map["wood"] = map["wood"] + collect
    elif collect_type == "g":
        map["coins"] = map["coins"] + collect
    elif collect_type == "c":
        playerInfo["cash"] = playerInfo["cash"] + collect
    elif collect_type == "s":
        map["stone"] = map["stone"] + collect
    elif collect_type == "f":
        map["food"] = map["food"] + collect

def apply_collect_xp(map: dict, id: int) -> None:
    collect_xp = int(get_attribute_from_item_id(id, "collect_xp"))
    map["xp"] = map["xp"] + collect_xp

def timestamp_now() -> int:
    return int(time.time())
