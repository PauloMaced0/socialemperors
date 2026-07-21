"""Tournament service endpoints (tournaments/*.php).

The client keeps polling tournaments/get_tournament_info.php when the player
opens the Tournament Arena; without a route it gets a 404, the popup never
opens and the request is retried forever. This module builds valid payloads
for the tournament service so the arena UI works.

Multiplayer matchmaking is not implemented: weekly tournaments are shown as
"available in <countdown>" (closed), and joining/creating a friends
tournament answers NOK ("room full") with a refund marker so the client
restores the entry fee. All payloads go through the client's
ServiceManager, which unwraps JSON.decode(payload)["data"].
"""
import datetime

from get_game_config import get_game_config


def _tournament_types() -> dict:
    return get_game_config().get("tournament_type", {})


def _seconds_to_next_monday() -> int:
    now = datetime.datetime.now()
    days = (7 - now.weekday()) % 7 or 7
    next_monday = datetime.datetime.combine(
        now.date() + datetime.timedelta(days=days), datetime.time.min)
    return int((next_monday - now).total_seconds())


def get_tournament_info() -> dict:
    """Payload for get_tournament_info.php: player not in any tournament.

    Keys read by TournamentManager/PopupTournament:
    - no "tournament" key -> isPlayerInTournament is false, list popup opens.
    - "tournament_weekly": per weekly type id, "open"/"full" flags and a
      "timeLeft" countdown in seconds. open="0" shows "available in ..."
      and hides the enter button (no weekly matchmaking on this server).
    - "tournament_reward_id": prize index per tournament type. Every
      non-private thumb resolves its reward through this map; a missing id
      would make the popup crash on prize[""]["u"]. Rotated weekly so the
      advertised prize unit changes like the original service did.
    - "tournament_friends": private tournaments created by friends (none).
    """
    week = datetime.date.today().isocalendar()[1]
    weekly = {}
    reward_id = {}
    for type_id, definition in _tournament_types().items():
        prizes = definition.get("prize") or []
        if prizes:
            reward_id[str(type_id)] = str(week % len(prizes))
        if int(definition.get("weekly_tournaments") or 0) >= 1:
            weekly[str(type_id)] = {
                "open": "0",
                "full": "0",
                "timeLeft": _seconds_to_next_monday(),
            }
    return {
        "tournament_friends": {},
        "tournament_weekly": weekly,
        "tournament_reward_id": reward_id,
    }


def join_tournament_full(tournament_type_id: str) -> dict:
    """NOK payload for join/create: client shows "room full" and refunds.

    The "resources" object drives evalRefundTournament, which gives back the
    gold/cash the client subtracted before calling the service.
    """
    return {
        "result": "NOK",
        "resources": {
            "refund": 1,
            "tournament_type_id": str(tournament_type_id),
        },
    }


def tournament_ok() -> dict:
    """OK payload for leave/cancel/clean (nothing to leave, nothing paid)."""
    return {"result": "OK", "resources": None}
