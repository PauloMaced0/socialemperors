"""Persistent shared tournament service used by the Flash Tournament Arena.

All eight tournament types rotate through one global, non-overlapping
schedule.  A tournament admits up to five real players for 24 hours, then
fills any empty positions with configured arena bots and runs its four-match
round robin for another 24 hours.  The shared room is stored beside village
saves, while each participant save keeps only its membership/history view.
Leaving consumes that cycle permanently and never refunds the entry fee.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
from pathlib import Path
import threading
import time
import uuid

import sessions
from get_game_config import get_game_config, get_level_from_xp


_LOCK = threading.RLock()
_DAILY_TYPE_IDS = tuple(str(value) for value in range(1, 8))
_WEEKLY_TYPE_ID = "8"
_ALL_TYPE_IDS = tuple(str(value) for value in range(1, 9))
_HISTORY_KEY = "tournamentHistory"
_ACTIVE_KEY = "activeTournament"
_ACTIVE_ID_KEY = "activeTournamentId"
_STATE_FILENAME = "tournaments.state.json"
_STATE_VERSION = 1
_MAX_PLAYERS = 5
_MAX_WINNER_HISTORY = 25
_ADMISSION_SECONDS = 24 * 3600
_BATTLE_SECONDS = 24 * 3600
# A deterministic local-midnight epoch makes the schedule survive restarts and
# keeps every server process/participant in the same two-day slot.
_ROTATION_EPOCH = datetime.date(2026, 8, 3)


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


def _state_path() -> Path:
    return Path(sessions.SAVES_DIR) / _STATE_FILENAME


def _empty_state() -> dict:
    return {
        "version": _STATE_VERSION,
        "rooms": {},
        "slots": {},
        "winner_history": [],
    }


def _load_state() -> dict:
    path = _state_path()
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_state()
    if not isinstance(value, dict):
        return _empty_state()
    if not isinstance(value.get("rooms"), dict):
        value["rooms"] = {}
    if not isinstance(value.get("slots"), dict):
        value["slots"] = {}
    if not isinstance(value.get("winner_history"), list):
        value["winner_history"] = []
    value["version"] = _STATE_VERSION
    return value


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(temporary, path)


def _midnight(value: datetime.date, timezone) -> datetime.datetime:
    return datetime.datetime.combine(value, datetime.time.min, tzinfo=timezone)


def _rotation(now: datetime.datetime) -> dict:
    current = _local_now(now)
    sequence = (current.date() - _ROTATION_EPOCH).days // 2
    start_date = _ROTATION_EPOCH + datetime.timedelta(days=sequence * 2)
    admission_start = _midnight(start_date, current.tzinfo)
    admission_end = admission_start + datetime.timedelta(seconds=_ADMISSION_SECONDS)
    battle_end = admission_end + datetime.timedelta(seconds=_BATTLE_SECONDS)
    type_id = _ALL_TYPE_IDS[sequence % len(_ALL_TYPE_IDS)]
    return {
        "sequence": sequence,
        "type_id": type_id,
        "slot": f"{start_date.isoformat()}:{type_id}",
        "admission_start": admission_start,
        "admission_end": admission_end,
        "battle_end": battle_end,
        "phase": "admission" if current < admission_end else "battle",
    }


def _next_admission(type_id: str, now: datetime.datetime) -> datetime.datetime:
    current = _rotation(now)
    target_index = _ALL_TYPE_IDS.index(str(type_id))
    current_index = current["sequence"] % len(_ALL_TYPE_IDS)
    delta = (target_index - current_index) % len(_ALL_TYPE_IDS)
    if delta == 0 and now >= current["admission_end"]:
        delta = len(_ALL_TYPE_IDS)
    start_date = _ROTATION_EPOCH + datetime.timedelta(
        days=(current["sequence"] + delta) * 2
    )
    return _midnight(start_date, now.tzinfo)


def _history(save: dict) -> dict:
    private = save.setdefault("privateState", {})
    value = private.setdefault(_HISTORY_KEY, {})
    if not isinstance(value, dict):
        value = {}
        private[_HISTORY_KEY] = value
    return value


def _room_for_slot(state: dict, slot: str) -> dict | None:
    room_id = str(state.get("slots", {}).get(str(slot), ""))
    room = state.get("rooms", {}).get(room_id)
    return room if isinstance(room, dict) else None


def _type_status(state: dict, save: dict | None, type_id: str,
                 now: datetime.datetime) -> dict:
    current = _rotation(now)
    history = _history(save) if save is not None else {}
    is_current_admission = (
        current["type_id"] == str(type_id)
        and current["phase"] == "admission"
    )
    consumed = str(history.get(str(type_id), "")) == current["slot"]
    room = _room_for_slot(state, current["slot"])
    participants = len((room or {}).get("players") or [])
    full = is_current_admission and participants >= _MAX_PLAYERS
    is_open = is_current_admission and not consumed and not full
    if is_open:
        target = current["admission_end"]
    else:
        target = _next_admission(str(type_id), now)
    return {
        "open": "1" if is_open else "0",
        "full": "1" if full else "0",
        "timeLeft": max(1, int((target - now).total_seconds())),
        "day": target.strftime("%d %b"),
        "phase": current["phase"] if current["type_id"] == str(type_id)
        else "scheduled",
        "players": participants if current["type_id"] == str(type_id) else 0,
        "maxPlayers": _MAX_PLAYERS,
    }


def _daily_status(state: dict, save: dict | None,
                  now: datetime.datetime) -> dict:
    return {
        type_id: _type_status(state, save, type_id, now)
        for type_id in _DAILY_TYPE_IDS
    }


def _weekly_status(state: dict, save: dict | None,
                   now: datetime.datetime) -> dict:
    return {
        _WEEKLY_TYPE_ID: _type_status(
            state, save, _WEEKLY_TYPE_ID, now
        )
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


def _bot_players(count: int, fallback_team: list[int]) -> list[dict]:
    weekly = _tournament_types().get(_WEEKLY_TYPE_ID, {})
    configured = weekly.get("weekly_opponent") or {}
    bots = []
    for index, bot_id in enumerate(list(configured)[:max(0, int(count))]):
        raw = configured[bot_id]
        team = _normalise_team(raw.get("team"))
        if not any(team):
            team = list(fallback_team)
        bots.append({
            "user_id": str(bot_id),
            "user_name": str(raw.get("user_name") or f"Arena Bot {index + 1}"),
            "country": str(raw.get("country") or ""),
            "level": int(raw.get("level", 1) or 1),
            "team": team,
            "matches": [],
            "abandonned": 0,
            "bot": 1,
        })
    return bots


def _human_player(user_id: str, save: dict, team: list[int]) -> dict:
    return {
        "user_id": user_id,
        "user_name": _display_name(save),
        "country": str(save.get("playerInfo", {}).get("country") or ""),
        "level": _player_level(save),
        "team": team,
        "matches": [],
        "abandonned": 0,
        "bot": 0,
    }


def _build_tournament(type_id: str, rotation: dict,
                      now: datetime.datetime) -> dict:
    now_ts = _unix(now)
    tournament = {
        "tournament_id": uuid.uuid4().hex,
        "tournament_type_id": type_id,
        "reward_id": _reward_ids(now).get(type_id, "0"),
        "private": "0",
        "owner_id": "",
        "owner": "",
        "point_type": 0,
        "slot": rotation["slot"],
        "admission_start": int(rotation["admission_start"].timestamp()),
        "admission_end": int(rotation["admission_end"].timestamp()),
        "battle_end": int(rotation["battle_end"].timestamp()),
        "date_created": now_ts,
        "date_ready": None,
        "date_finished": None,
        "match_playing": False,
        "active_matches": [],
        "players": [],
        "ranking": {},
        "rewards_credited": {},
    }
    return tournament


def _player_is_bot(player: dict) -> bool:
    return bool(int(player.get("bot", 0) or 0)) or str(
        player.get("user_id", "")
    ).startswith("1000000")


def _start_room_battles(tournament: dict) -> None:
    """Freeze the five-player roster and build every directed match once."""
    players = tournament.setdefault("players", [])
    fallback = next((value.get("team") for value in players
                     if any(value.get("team") or [])), [0] * 20)
    existing_ids = {str(value.get("user_id")) for value in players}
    candidates = [
        value for value in _bot_players(_MAX_PLAYERS, fallback)
        if str(value.get("user_id")) not in existing_ids
    ]
    players.extend(candidates[:max(0, _MAX_PLAYERS - len(players))])
    players[:] = players[:_MAX_PLAYERS]
    battle_start = int(tournament.get("admission_end", 0) or 0)
    bot_scores = (75.0, 70.0, 65.0, 60.0)
    bot_index = 0
    for player in players:
        attacker = str(player.get("user_id"))
        old = {
            str(value.get("victim_id")): value
            for value in player.get("matches") or []
        }
        matches = []
        is_bot = _player_is_bot(player)
        for match_index, victim in enumerate(players):
            victim_id = str(victim.get("user_id"))
            if victim_id == attacker:
                continue
            if victim_id in old:
                matches.append(old[victim_id])
            elif is_bot:
                started = battle_start + bot_index * 30 + match_index * 75
                matches.append(_match(
                    attacker, victim_id,
                    points=bot_scores[min(bot_index, len(bot_scores) - 1)],
                    started=started,
                    duration=90 + bot_index * 5 + match_index,
                ))
            else:
                matches.append(_match(attacker, victim_id))
        player["matches"] = matches
        if is_bot:
            bot_index += 1
    tournament["date_ready"] = battle_start


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
    return len(matches) >= _MAX_PLAYERS - 1 and all(
        bool(value.get("finished")) for value in matches
    )


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


def _credit_reward(save: dict, tournament: dict, user_id: str) -> bool:
    credited = tournament.setdefault("rewards_credited", {})
    if (
        credited.get(str(user_id))
        or not _all_player_matches_finished(tournament, str(user_id))
        or not _player_won(tournament, str(user_id))
    ):
        return False
    definition = _tournament_types().get(str(tournament["tournament_type_id"]), {})
    prizes = definition.get("prize") or []
    if not prizes:
        credited[str(user_id)] = True
        return True
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
    credited[str(user_id)] = True
    return True


def _winner(tournament: dict) -> dict | None:
    """Return the authoritative first-place player after ranking refresh."""
    _update_ranking(tournament, "")
    first = tournament.get("ranking", {}).get("top", {}).get("1")
    if not isinstance(first, dict):
        return None
    return _participant(tournament, str(first.get("user_id") or ""))


def _record_winner(state: dict, tournament: dict, winner: dict) -> bool:
    tournament_id = str(tournament.get("tournament_id") or "")
    history = state.setdefault("winner_history", [])
    if any(
        str(value.get("tournament_id") or "") == tournament_id
        for value in history if isinstance(value, dict)
    ):
        tournament["winner_recorded"] = True
        return False
    type_id = str(tournament.get("tournament_type_id") or "")
    definition = _tournament_types().get(type_id, {})
    score, total_time, matches = _player_score(winner)
    history.insert(0, {
        "tournament_id": tournament_id,
        "tournament_type_id": type_id,
        "tournament_name": str(
            definition.get("name") or f"Tournament {type_id}"
        ),
        "winner_id": str(winner.get("user_id") or ""),
        "winner_name": str(winner.get("user_name") or "Emperor"),
        "points": score,
        "time": total_time,
        "matches": matches,
        "finished_at": int(tournament.get("date_finished") or 0),
        "bot": 1 if _player_is_bot(winner) else 0,
    })
    del history[_MAX_WINNER_HISTORY:]
    tournament["winner_recorded"] = True
    return True


def _settle_room(state: dict, tournament: dict,
                 now: datetime.datetime) -> bool:
    """Record the result and deliver first prize once the deadline passes.

    Settlement is shared-room work, rather than work belonging to the player
    who happened to poll the endpoint.  Consequently any arena refresh can
    credit the real winner, even if a different participant is the first one
    to return after the tournament ends.
    """
    if tournament.get("date_finished") is None:
        return False
    winner = _winner(tournament)
    if winner is None:
        return False
    changed = _record_winner(state, tournament, winner)
    winner_id = str(winner.get("user_id") or "")
    if _player_is_bot(winner):
        if not tournament.get("settlement_complete"):
            tournament["settlement_complete"] = True
            changed = True
        return changed
    if tournament.setdefault("rewards_credited", {}).get(winner_id):
        if not tournament.get("settlement_complete"):
            tournament["settlement_complete"] = True
            changed = True
        return changed
    with sessions.session_lock(winner_id):
        winner_save = sessions.session(winner_id)
        if winner_save is None:
            return changed
        credited = _credit_reward(winner_save, tournament, winner_id)
        if credited:
            _set_active_view(winner_save, tournament, winner_id, now)
            sessions.save_session(winner_id)
            changed = True
    if tournament.setdefault("rewards_credited", {}).get(winner_id):
        tournament["settlement_complete"] = True
        changed = True
    return changed


def _refresh_rooms(state: dict, now: datetime.datetime) -> bool:
    changed = False
    for tournament in list(state.setdefault("rooms", {}).values()):
        if not isinstance(tournament, dict):
            continue
        changed = _refresh_room(tournament, now) or changed
        changed = _settle_room(state, tournament, now) or changed
    return changed


def _room_view(tournament: dict, user_id: str,
               now: datetime.datetime) -> dict:
    """Return a participant-specific copy safe to put in a village save."""
    view = copy.deepcopy(tournament)
    _update_ranking(view, str(user_id))
    credited = view.pop("rewards_credited", {})
    view["reward_credited"] = bool(credited.get(str(user_id)))
    now_ts = _unix(now)
    if view.get("date_finished") is not None:
        phase = "finished"
    elif view.get("date_ready") is None:
        phase = "admission"
    else:
        phase = "battle"
    view["phase"] = phase
    view["admission_time_left"] = max(
        0, int(view.get("admission_end", 0) or 0) - now_ts,
    )
    view["battle_time_left"] = max(
        0, int(view.get("battle_end", 0) or 0) - now_ts,
    ) if phase == "battle" else 0
    return view


def _set_active_view(save: dict, tournament: dict, user_id: str,
                     now: datetime.datetime) -> None:
    private = save.setdefault("privateState", {})
    private[_ACTIVE_ID_KEY] = str(tournament.get("tournament_id"))
    private[_ACTIVE_KEY] = _room_view(tournament, user_id, now)


def _clear_active(save: dict) -> None:
    private = save.setdefault("privateState", {})
    private.pop(_ACTIVE_ID_KEY, None)
    private.pop(_ACTIVE_KEY, None)


def _find_room(state: dict, save: dict, user_id: str,
               now: datetime.datetime) -> tuple[dict | None, bool]:
    """Resolve the authoritative room and migrate old save-local brackets."""
    private = save.setdefault("privateState", {})
    room_id = str(private.get(_ACTIVE_ID_KEY) or "")
    legacy = private.get(_ACTIVE_KEY)
    if not room_id and isinstance(legacy, dict):
        room_id = str(legacy.get("tournament_id") or "")
    room = state.setdefault("rooms", {}).get(room_id)
    if isinstance(room, dict):
        return room, False
    if not room_id or not isinstance(legacy, dict):
        return None, False

    # Versions before shared rooms stored an immediately-ready four-player
    # bracket inside the village. Preserve it rather than deleting paid entry.
    room = copy.deepcopy(legacy)
    ready = int(room.get("date_ready") or _unix(now))
    room.update({
        "slot": str(room.get("slot") or f"legacy:{room_id}"),
        "admission_start": int(room.get("date_created") or ready),
        "admission_end": ready,
        "battle_end": int(room.get("battle_end") or ready + _BATTLE_SECONDS),
        "active_matches": [],
        "rewards_credited": {
            str(user_id): bool(room.pop("reward_credited", False)),
        },
    })
    for player in room.get("players") or []:
        player["bot"] = int(
            player.get("bot", 1 if str(player.get("user_id")) != user_id else 0)
            or 0
        )
    _start_room_battles(room)
    state["rooms"][room_id] = room
    private[_ACTIVE_ID_KEY] = room_id
    return room, True


def _refresh_room(tournament: dict, now: datetime.datetime) -> bool:
    """Advance admission/battle phases according to persisted timestamps."""
    changed = False
    now_ts = _unix(now)
    if (
        tournament.get("date_ready") is None
        and now_ts >= int(tournament.get("admission_end", 0) or 0)
    ):
        _start_room_battles(tournament)
        changed = True
    if (
        tournament.get("date_ready") is not None
        and tournament.get("date_finished") is None
        and now_ts >= int(tournament.get("battle_end", 0) or 0)
    ):
        tournament["date_finished"] = int(
            tournament.get("battle_end", 0) or now_ts
        )
        tournament["match_playing"] = False
        tournament["active_matches"] = []
        changed = True
    return changed


def _participant(tournament: dict, user_id: str) -> dict | None:
    return next((
        value for value in tournament.get("players") or []
        if str(value.get("user_id")) == str(user_id)
    ), None)


def _get_tournament_info_locked(user_id: str | None,
                                now: datetime.datetime) -> dict:
    save = sessions.session(str(user_id)) if user_id is not None else None
    state = _load_state()
    state_changed = _refresh_rooms(state, now)
    save_changed = False
    tournament = None
    if save is not None:
        tournament, migrated = _find_room(state, save, str(user_id), now)
        state_changed = state_changed or migrated
        if tournament is not None:
            state_changed = _refresh_room(tournament, now) or state_changed
            state_changed = _settle_room(state, tournament, now) or state_changed
            fresh_view = _room_view(tournament, str(user_id), now)
            private = save.setdefault("privateState", {})
            old_view = private.get(_ACTIVE_KEY)
            # The room popup polls once per minute. Do not rewrite a large
            # village save merely because its display countdown changed; the
            # authoritative current view is already returned below. Persist
            # only lifecycle facts needed to recover membership after restart.
            lifecycle_keys = (
                "tournament_id", "date_ready", "date_finished",
                "reward_credited",
            )
            if (
                migrated
                or str(private.get(_ACTIVE_ID_KEY) or "")
                    != str(tournament.get("tournament_id") or "")
                or not isinstance(old_view, dict)
                or any(old_view.get(key) != fresh_view.get(key)
                       for key in lifecycle_keys)
            ):
                private[_ACTIVE_ID_KEY] = str(tournament.get("tournament_id"))
                private[_ACTIVE_KEY] = fresh_view
                save_changed = True
    payload = {
        "tournament_friends": {},
        "tournament_daily": _daily_status(state, save, now),
        "tournament_weekly": _weekly_status(state, save, now),
        "tournament_reward_id": _reward_ids(now),
        "tournament_winner_history": copy.deepcopy(
            state.get("winner_history", [])[:_MAX_WINNER_HISTORY]
        ),
    }
    if tournament is not None:
        payload["tournament"] = _room_view(tournament, str(user_id), now)
    if state_changed:
        _save_state(state)
    if save_changed and user_id is not None:
        sessions.save_session(str(user_id))
    return payload


def get_tournament_info(user_id: str | None = None,
                        now: datetime.datetime | None = None) -> dict:
    """Return exactly the state consumed by ``TournamentManager``."""
    current = _local_now(now)
    with _LOCK, sessions.session_lock(str(user_id)):
        return _get_tournament_info_locked(user_id, current)


def _refund(type_id: str, result: str = "NOK") -> dict:
    return {
        "result": result,
        "resources": {"refund": 1, "tournament_type_id": str(type_id)},
    }


def join_tournament(user_id: str, tournament_type_id: str, team,
                    now: datetime.datetime | None = None) -> dict:
    """Join the current shared admission room without starting it early."""
    user_id = str(user_id)
    type_id = str(tournament_type_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        definition = _tournament_types().get(type_id)
        if save is None or definition is None:
            return _refund(type_id)
        state = _load_state()
        existing, migrated = _find_room(state, save, user_id, current)
        if migrated:
            _save_state(state)
        if isinstance(existing, dict):
            # A replayed HTTP request must reopen the same room, not spend a
            # second fee or create a second prize opportunity.
            return _get_tournament_info_locked(user_id, current)
        normalised_team = _normalise_team(team)
        if not any(normalised_team):
            return _refund(type_id, "NOTEAM")
        rotation = _rotation(current)
        if type_id not in _ALL_TYPE_IDS or rotation["type_id"] != type_id:
            return _refund(type_id)
        status = _type_status(state, save, type_id, current)
        if status["open"] != "1":
            return _refund(
                type_id, "FULL" if status["full"] == "1" else "NOK",
            )
        tournament = _room_for_slot(state, rotation["slot"])
        if tournament is None:
            tournament = _build_tournament(type_id, rotation, current)
            state["rooms"][tournament["tournament_id"]] = tournament
            state["slots"][rotation["slot"]] = tournament["tournament_id"]
        _refresh_room(tournament, current)
        if tournament.get("date_ready") is not None:
            return _refund(type_id)
        if len(tournament.get("players") or []) >= _MAX_PLAYERS:
            return _refund(type_id, "FULL")
        player = _human_player(user_id, save, normalised_team)
        tournament.setdefault("players", []).append(player)
        if not tournament.get("owner_id"):
            tournament["owner_id"] = user_id
            tournament["owner"] = player["user_name"]
        _history(save)[type_id] = rotation["slot"]
        _set_active_view(save, tournament, user_id, current)
        _save_state(state)
        sessions.save_session(user_id)
        return _get_tournament_info_locked(user_id, current)


def start_tournament_match(user_id: str, tournament_id: str,
                           victim_id: str,
                           now: datetime.datetime | None = None) -> dict:
    user_id = str(user_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        if save is None:
            return {"result": "NOK"}
        state = _load_state()
        tournament, migrated = _find_room(state, save, user_id, current)
        if not isinstance(tournament, dict) or str(tournament.get("tournament_id")) != str(tournament_id):
            return {"result": "NOK"}
        changed = _refresh_room(tournament, current) or migrated
        player = _participant(tournament, user_id)
        if (
            player is None
            or bool(player.get("abandonned"))
            or tournament.get("date_ready") is None
            or tournament.get("date_finished") is not None
        ):
            if changed:
                _save_state(state)
            result = "WAITING" if tournament.get("date_ready") is None else "NOK"
            return {"result": result}
        match = next((value for value in player.get("matches", [])
                      if str(value.get("victim_id")) == str(victim_id)), None)
        if match is None:
            return {"result": "NOK"}
        if not match.get("finished") and match.get("date_started") is None:
            match["date_started"] = _unix(current)
        active = tournament.setdefault("active_matches", [])
        if not match.get("finished") and user_id not in active:
            active.append(user_id)
        tournament["match_playing"] = bool(active)
        _set_active_view(save, tournament, user_id, current)
        _save_state(state)
        sessions.save_session(user_id)
        return {"result": "OK"}


def finish_tournament_match(user_id: str, tournament_id: str,
                            victim_id: str, attacker_won, attacker_points,
                            now: datetime.datetime | None = None) -> dict:
    user_id = str(user_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        if save is None:
            return {"result": "NOK"}
        state = _load_state()
        tournament, migrated = _find_room(state, save, user_id, current)
        if not isinstance(tournament, dict) or str(tournament.get("tournament_id")) != str(tournament_id):
            return {"result": "NOK"}
        changed = _refresh_room(tournament, current) or migrated
        player = _participant(tournament, user_id)
        if (
            player is None
            or bool(player.get("abandonned"))
            or tournament.get("date_ready") is None
            or tournament.get("date_finished") is not None
        ):
            if changed:
                _save_state(state)
            result = "WAITING" if tournament.get("date_ready") is None else "NOK"
            return {"result": result}
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
        active = tournament.setdefault("active_matches", [])
        active[:] = [value for value in active if str(value) != user_id]
        tournament["match_playing"] = bool(active)
        _update_ranking(tournament, user_id)
        _set_active_view(save, tournament, user_id, current)
        _save_state(state)
        sessions.save_session(user_id)
        return {"result": "OK"}


def leave_tournament(user_id: str,
                     now: datetime.datetime | None = None) -> dict:
    """Leave a started room.  Its entry slot remains consumed, with no refund."""
    user_id = str(user_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        if save is not None:
            state = _load_state()
            tournament, migrated = _find_room(state, save, user_id, current)
            if isinstance(tournament, dict):
                _refresh_room(tournament, current)
                player = _participant(tournament, user_id)
                if player is not None and tournament.get("date_ready") is None:
                    tournament["players"] = [
                        value for value in tournament.get("players") or []
                        if str(value.get("user_id")) != user_id
                    ]
                    if str(tournament.get("owner_id")) == user_id:
                        replacement = next(iter(tournament["players"]), {})
                        tournament["owner_id"] = str(
                            replacement.get("user_id") or ""
                        )
                        tournament["owner"] = str(
                            replacement.get("user_name") or ""
                        )
                elif player is not None:
                    player["abandonned"] = 1
                    stamp = _unix(current)
                    for match in player.get("matches") or []:
                        if not match.get("finished"):
                            match.update({
                                "attacker_won": False,
                                "attacker_points": 0,
                                "victim_points": 0,
                                "date_started": match.get("date_started") or stamp,
                                "date_finished": stamp,
                                "finished": True,
                            })
                    active = tournament.setdefault("active_matches", [])
                    active[:] = [
                        value for value in active if str(value) != user_id
                    ]
                    tournament["match_playing"] = bool(active)
                _save_state(state)
            elif migrated:
                _save_state(state)
            _clear_active(save)
            sessions.save_session(user_id)
    return {"result": "OK", "resources": None}


def clean_tournament(user_id: str,
                     now: datetime.datetime | None = None) -> dict:
    """Dismiss a finished bracket after its server-credited reward is shown."""
    user_id = str(user_id)
    current = _local_now(now)
    with _LOCK, sessions.session_lock(user_id):
        save = sessions.session(user_id)
        if save is None:
            return {"result": "NOK", "resources": None}
        state = _load_state()
        tournament, migrated = _find_room(state, save, user_id, current)
        if isinstance(tournament, dict):
            changed = _refresh_room(tournament, current) or migrated
            if changed:
                _save_state(state)
        if isinstance(tournament, dict) and tournament.get("date_finished") is None:
            return {"result": "NOK", "resources": None}
        _clear_active(save)
        sessions.save_session(user_id)
    return {"result": "OK", "resources": None}


# Compatibility helpers retained for older imports/tests.
def join_tournament_full(tournament_type_id: str) -> dict:
    return _refund(str(tournament_type_id))


def tournament_ok() -> dict:
    return {"result": "OK", "resources": None}
