"""Persistent local tournament service used by the Flash Tournament Arena.

The original Social Empires client delegates tournament rooms, matches and
rankings to ``tournaments/*.php`` services.  This server used to answer every
join with ``NOK``, which the client presents as "room full".  Local brackets
now pair the player with the three opponents bundled in the game data, persist
the bracket in the player's save, and implement the start/finish lifecycle the
client expects.

Types 1-7 rotate Monday-Sunday (one open bracket per local calendar day).
Weekly Gold remains open for the ISO week.  A slot can be entered once, so a
reload or a replayed request cannot mint another prize.  Weekly Gold requires
all three matches and first place against the configured bots; merely having a
small number of human save files therefore does not guarantee its dragon.
"""
from __future__ import annotations

import copy
import datetime
import threading
import time
import uuid

import sessions
from get_game_config import get_game_config, get_level_from_xp


_LOCK = threading.RLock()
_DAILY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday",
)
_DAILY_TYPE_IDS = tuple(str(value) for value in range(1, 8))
_WEEKLY_TYPE_ID = "8"
_HISTORY_KEY = "tournamentHistory"
_ACTIVE_KEY = "activeTournament"


def _tournament_types() -> dict:
    return get_game_config().get("tournament_type", {})


def _local_now(now: datetime.datetime | None = None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now.astimezone()


def _unix(now: datetime.datetime | None = None) -> int:
    return int(_local_now(now).timestamp()) if now is not None else int(time.time())


def _next_midnight(now: datetime.datetime) -> datetime.datetime:
    return datetime.datetime.combine(
        now.date() + datetime.timedelta(days=1),
        datetime.time.min,
        tzinfo=now.tzinfo,
    )


def _seconds_to_next_monday(now: datetime.datetime | None = None) -> int:
    current = _local_now(now)
    days = (7 - current.weekday()) % 7 or 7
    target = datetime.datetime.combine(
        current.date() + datetime.timedelta(days=days),
        datetime.time.min,
        tzinfo=current.tzinfo,
    )
    return max(1, int((target - current).total_seconds()))


def _week_slot(now: datetime.datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _history(save: dict) -> dict:
    private = save.setdefault("privateState", {})
    value = private.setdefault(_HISTORY_KEY, {})
    if not isinstance(value, dict):
        value = {}
        private[_HISTORY_KEY] = value
    return value


def _daily_status(save: dict | None, now: datetime.datetime) -> dict:
    history = _history(save) if save is not None else {}
    status = {}
    for index, type_id in enumerate(_DAILY_TYPE_IDS):
        is_today = now.weekday() == index
        today_slot = now.date().isoformat()
        used_today = str(history.get(type_id, "")) == today_slot
        is_open = is_today and not used_today
        if is_open:
            target = _next_midnight(now)
        else:
            days = (index - now.weekday()) % 7
            if days == 0:
                days = 7
            target = datetime.datetime.combine(
                now.date() + datetime.timedelta(days=days),
                datetime.time.min,
                tzinfo=now.tzinfo,
            )
        status[type_id] = {
            "open": "1" if is_open else "0",
            "full": "0",
            "timeLeft": max(1, int((target - now).total_seconds())),
            "day": _DAILY_NAMES[index],
        }
    return status


def _weekly_status(save: dict | None, now: datetime.datetime) -> dict:
    history = _history(save) if save is not None else {}
    used = str(history.get(_WEEKLY_TYPE_ID, "")) == _week_slot(now)
    return {
        _WEEKLY_TYPE_ID: {
            "open": "0" if used else "1",
            "full": "0",
            "timeLeft": _seconds_to_next_monday(now),
        }
    }


def _reward_ids(now: datetime.datetime) -> dict:
    week = now.isocalendar().week
    reward_id = {}
    for type_id, definition in _tournament_types().items():
        prizes = definition.get("prize") or []
        if prizes:
            reward_id[str(type_id)] = str(week % len(prizes))
    return reward_id


def _display_name(save: dict) -> str:
    info = save.get("playerInfo", {})
    names = info.get("map_names") or []
    default = int(info.get("default_map", 0) or 0)
    if 0 <= default < len(names) and names[default]:
        return str(names[default])
    return str(info.get("name") or "Emperor")


def _player_level(save: dict) -> int:
    info = save.get("playerInfo", {})
    maps = save.get("maps") or [{}]
    default = int(info.get("default_map", 0) or 0)
    town = maps[default] if 0 <= default < len(maps) else maps[0]
    return get_level_from_xp(int(town.get("xp", 0) or 0))


def _normalise_team(value) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for raw in value[:20]:
        try:
            result.append(max(0, int(raw)))
        except (TypeError, ValueError):
            result.append(0)
    result.extend([0] * (20 - len(result)))
    return result


def _match(attacker: str, victim: str, *, points: float | None = None,
           started: int | None = None, duration: int = 0) -> dict:
    finished = points is not None
    return {
        "attacker_id": str(attacker),
        "victim_id": str(victim),
        "attacker_won": bool(finished and float(points) >= 50),
        "attacker_points": 0 if points is None else round(float(points), 2),
        "victim_points": 0,
        "date_started": started,
        "date_finished": (started + duration) if finished and started else None,
        "finished": finished,
    }


def _bot_players(definition: dict, user_id: str, player_team: list[int],
                 now_ts: int) -> list[dict]:
    weekly = _tournament_types().get(_WEEKLY_TYPE_ID, {})
    configured = weekly.get("weekly_opponent") or {}
    # Totals are 225, 210 and 195.  The weekly dragon therefore requires the
    # player to average above 75 points across all three battles (ties are
    # broken by total time), rather than being an automatic small-server gift.
    scores = (75.0, 70.0, 65.0)
    bots = []
    ids = list(configured)[:3]
    for index, bot_id in enumerate(ids):
        raw = configured[bot_id]
        team = _normalise_team(raw.get("team"))
        if not any(team):
            team = list(player_team)
        opponents = [user_id] + [value for value in ids if value != bot_id]
        matches = []
        for match_index, victim in enumerate(opponents[:3]):
            started = now_ts - 600 + index * 30 + match_index * 75
            matches.append(_match(
                bot_id, victim, points=scores[index], started=started,
                duration=90 + index * 5 + match_index,
            ))
        bots.append({
            "user_id": str(bot_id),
            "user_name": str(raw.get("user_name") or f"Arena Bot {index + 1}"),
            "country": str(raw.get("country") or ""),
            "level": int(raw.get("level", 1) or 1),
            "team": team,
            "matches": matches,
            "abandonned": 0,
        })
    return bots


def _build_tournament(user_id: str, save: dict, type_id: str, team: list[int],
                      now: datetime.datetime) -> dict:
    definition = _tournament_types()[type_id]
    now_ts = _unix(now)
    bots = _bot_players(definition, user_id, team, now_ts)
    player_matches = [_match(user_id, bot["user_id"]) for bot in bots]
    player = {
        "user_id": user_id,
        "user_name": _display_name(save),
        "country": str(save.get("playerInfo", {}).get("country") or ""),
        "level": _player_level(save),
        "team": team,
        "matches": player_matches,
        "abandonned": 0,
    }
    tournament = {
        "tournament_id": uuid.uuid4().hex,
        "tournament_type_id": type_id,
        "reward_id": _reward_ids(now).get(type_id, "0"),
        "private": "0",
        "owner_id": user_id,
        "owner": player["user_name"],
        "point_type": 0,
        "date_ready": now_ts,
        "date_finished": None,
        "match_playing": False,
        "players": [player] + bots,
        "ranking": {},
        "reward_credited": False,
    }
    _update_ranking(tournament, user_id)
    return tournament


def _player_score(player: dict) -> tuple[float, int, int]:
    score = 0.0
    total_time = 0
    played = 0
    for match in player.get("matches") or []:
        if not bool(match.get("finished")):
            continue
        score += float(match.get("attacker_points", 0) or 0)
        try:
            total_time += max(
                0,
                int(match.get("date_finished") or 0)
                - int(match.get("date_started") or 0),
            )
        except (TypeError, ValueError):
            pass
        played += 1
    return round(score, 2), total_time, played


def _update_ranking(tournament: dict, user_id: str) -> None:
    rows = []
    for player in tournament.get("players") or []:
        score, total_time, played = _player_score(player)
        rows.append({
            "user_id": str(player.get("user_id")),
            "user_name": str(player.get("user_name") or "Emperor"),
            "points": score,
            "time": total_time,
            "matches": played,
        })
    rows.sort(key=lambda row: (-row["points"], row["time"], row["user_id"]))
    top = {}
    user = None
    for rank, row in enumerate(rows, 1):
        ranked = dict(row, rank=str(rank))
        top[str(rank)] = dict(row)
        if row["user_id"] == user_id:
            user = ranked
    tournament["ranking"] = {
        "top": top,
        "user": user or {
            "user_id": user_id, "user_name": "Emperor", "points": 0,
            "time": 0, "matches": 0, "rank": str(len(rows) + 1),
        },
        "total_user": len(rows),
    }


def _all_player_matches_finished(tournament: dict, user_id: str) -> bool:
    player = next((value for value in tournament.get("players", [])
                   if str(value.get("user_id")) == user_id), None)
    matches = (player or {}).get("matches") or []
    return len(matches) >= 3 and all(bool(value.get("finished")) for value in matches)


def _player_won(tournament: dict, user_id: str) -> bool:
    _update_ranking(tournament, user_id)
    return str(tournament["ranking"]["user"].get("rank")) == "1"


def _add_stored_unit(save: dict, unit_id: int, count: int = 1) -> None:
    town = save["maps"][0]
    raw = town.get("store")
    if not isinstance(raw, dict):
        raw = {}
        town["store"] = raw
    key = str(int(unit_id))
    raw[key] = int(raw.get(key, 0) or 0) + int(count)


def _credit_reward(save: dict, tournament: dict, user_id: str) -> None:
    if tournament.get("reward_credited") or not _player_won(tournament, user_id):
        return
    definition = _tournament_types().get(str(tournament["tournament_type_id"]), {})
    prizes = definition.get("prize") or []
    if not prizes:
        tournament["reward_credited"] = True
        return
    if int(definition.get("weekly_tournaments", 0) or 0) >= 1:
        # Weekly Gold is first-place-only on the local server.
        index = 0
    else:
        index = min(max(int(tournament.get("reward_id", 0) or 0), 0), len(prizes) - 1)
    prize = prizes[index]
    save["maps"][0]["coins"] = int(save["maps"][0].get("coins", 0) or 0) \
        + int(prize.get("g", 0) or 0)
    save["playerInfo"]["cash"] = int(save["playerInfo"].get("cash", 0) or 0) \
        + int(prize.get("c", 0) or 0)
    for raw_id, raw_count in (prize.get("u") or {}).items():
        _add_stored_unit(save, int(raw_id), int(raw_count))
    tournament["reward_credited"] = True


def _refresh_tournament(save: dict, user_id: str,
                        now: datetime.datetime) -> bool:
    tournament = save.setdefault("privateState", {}).get(_ACTIVE_KEY)
    if not isinstance(tournament, dict):
        return False
    changed = False
    _update_ranking(tournament, user_id)
    definition = _tournament_types().get(str(tournament.get("tournament_type_id")), {})
    expires = int(tournament.get("date_ready", 0) or 0) \
        + int(definition.get("duration", 24) or 24) * 3600
    if tournament.get("date_finished") is None and (
        _all_player_matches_finished(tournament, user_id) or _unix(now) >= expires
    ):
        tournament["date_finished"] = _unix(now)
        tournament["match_playing"] = False
        changed = True
    if tournament.get("date_finished") is not None:
        before = bool(tournament.get("reward_credited"))
        _credit_reward(save, tournament, user_id)
        changed = changed or before != bool(tournament.get("reward_credited"))
    return changed


def get_tournament_info(user_id: str | None = None,
                        now: datetime.datetime | None = None) -> dict:
    """Return exactly the state consumed by ``TournamentManager``."""
    current = _local_now(now)
    save = sessions.session(str(user_id)) if user_id is not None else None
    with _LOCK, sessions.session_lock(str(user_id)):
        if save is not None and _refresh_tournament(save, str(user_id), current):
            sessions.save_session(str(user_id))
        payload = {
            "tournament_friends": {},
            "tournament_daily": _daily_status(save, current),
            "tournament_weekly": _weekly_status(save, current),
            "tournament_reward_id": _reward_ids(current),
        }
        tournament = (save or {}).get("privateState", {}).get(_ACTIVE_KEY)
        if isinstance(tournament, dict):
            payload["tournament"] = copy.deepcopy(tournament)
        return payload


def _refund(type_id: str, result: str = "NOK") -> dict:
    return {
        "result": result,
        "resources": {"refund": 1, "tournament_type_id": str(type_id)},
    }


def join_tournament(user_id: str, tournament_type_id: str, team,
                    now: datetime.datetime | None = None) -> dict:
    """Enter the currently open slot and return its persistent bot bracket."""
    user_id = str(user_id)
    type_id = str(tournament_type_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        definition = _tournament_types().get(type_id)
        if save is None or definition is None:
            return _refund(type_id)
        existing = save.setdefault("privateState", {}).get(_ACTIVE_KEY)
        if isinstance(existing, dict):
            # A replayed HTTP request must reopen the same room, not spend a
            # second fee or create a second prize opportunity.
            return get_tournament_info(user_id, current)
        normalised_team = _normalise_team(team)
        if not any(normalised_team):
            return _refund(type_id, "NOTEAM")
        if type_id in _DAILY_TYPE_IDS:
            if _daily_status(save, current)[type_id]["open"] != "1":
                return _refund(type_id)
            slot = current.date().isoformat()
        elif type_id == _WEEKLY_TYPE_ID:
            if _weekly_status(save, current)[type_id]["open"] != "1":
                return _refund(type_id)
            slot = _week_slot(current)
        else:
            return _refund(type_id)
        tournament = _build_tournament(
            user_id, save, type_id, normalised_team, current,
        )
        save["privateState"][_ACTIVE_KEY] = tournament
        _history(save)[type_id] = slot
        sessions.save_session(user_id)
        return get_tournament_info(user_id, current)


def start_tournament_match(user_id: str, tournament_id: str,
                           victim_id: str,
                           now: datetime.datetime | None = None) -> dict:
    user_id = str(user_id)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        tournament = (save or {}).get("privateState", {}).get(_ACTIVE_KEY)
        if not isinstance(tournament, dict) or str(tournament.get("tournament_id")) != str(tournament_id):
            return {"result": "NOK"}
        player = tournament.get("players", [None])[0]
        match = next((value for value in player.get("matches", [])
                      if str(value.get("victim_id")) == str(victim_id)), None)
        if match is None:
            return {"result": "NOK"}
        if not match.get("finished") and match.get("date_started") is None:
            match["date_started"] = _unix(now)
        tournament["match_playing"] = not bool(match.get("finished"))
        sessions.save_session(user_id)
        return {"result": "OK"}


def finish_tournament_match(user_id: str, tournament_id: str,
                            victim_id: str, attacker_won, attacker_points,
                            now: datetime.datetime | None = None) -> dict:
    user_id = str(user_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        tournament = (save or {}).get("privateState", {}).get(_ACTIVE_KEY)
        if not isinstance(tournament, dict) or str(tournament.get("tournament_id")) != str(tournament_id):
            return {"result": "NOK"}
        player = tournament.get("players", [None])[0]
        match = next((value for value in player.get("matches", [])
                      if str(value.get("victim_id")) == str(victim_id)), None)
        if match is None:
            return {"result": "NOK"}
        if match.get("finished"):
            # Idempotent retry after a lost HTTP response.
            return {"result": "OK"}
        try:
            points = max(0.0, min(100.0, float(attacker_points)))
        except (TypeError, ValueError):
            points = 0.0
        if match.get("date_started") is None:
            match["date_started"] = _unix(current)
        won = attacker_won is True or str(attacker_won).lower() in ("1", "true", "yes")
        match.update({
            "attacker_won": won,
            "attacker_points": round(points, 2),
            "victim_points": 0,
            "date_finished": _unix(current),
            "finished": True,
        })
        tournament["match_playing"] = False
        _refresh_tournament(save, user_id, current)
        sessions.save_session(user_id)
        return {"result": "OK"}


def leave_tournament(user_id: str) -> dict:
    """Leave a started room.  Its entry slot remains consumed, with no refund."""
    user_id = str(user_id)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        if save is not None:
            save.setdefault("privateState", {}).pop(_ACTIVE_KEY, None)
            sessions.save_session(user_id)
    return {"result": "OK", "resources": None}


def clean_tournament(user_id: str) -> dict:
    """Dismiss a finished bracket after its server-credited reward is shown."""
    user_id = str(user_id)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        tournament = (save or {}).get("privateState", {}).get(_ACTIVE_KEY)
        if isinstance(tournament, dict) and tournament.get("date_finished") is None:
            return {"result": "NOK", "resources": None}
        if save is not None:
            save.setdefault("privateState", {}).pop(_ACTIVE_KEY, None)
            sessions.save_session(user_id)
    return {"result": "OK", "resources": None}


# Compatibility helpers retained for older imports/tests.
def join_tournament_full(tournament_type_id: str) -> dict:
    return _refund(str(tournament_type_id))


def tournament_ok() -> dict:
    return {"result": "OK", "resources": None}
