"""Read queries used by dashboard pages.

Everything here is pure SQL over the shared bot DB. No mutation — control-plane
writes go through `dashboard.commands`.
"""

from __future__ import annotations

from dataclasses import dataclass

from db.database import Database
from db.models import BotStateKey


@dataclass(slots=True, frozen=True)
class NowPlayingRow:
    track_id: str | None
    title: str | None
    playlist_position: int
    playback_position_seconds: int
    is_paused: bool


@dataclass(slots=True, frozen=True)
class WatcherRow:
    user_id: str
    username: str
    server_nickname: str | None
    joined_at: str
    seconds_so_far: int


@dataclass(slots=True, frozen=True)
class LeaderboardRow:
    rank: int
    user_id: str
    username: str
    server_nickname: str | None
    seconds: int


def now_playing(db: Database) -> NowPlayingRow:
    tid = db.get_state(BotStateKey.CURRENT_TRACK_ID)
    # We don't have track titles in the bot DB (that's the provider's job).
    # The dashboard route enriches with the file-provider client separately.
    return NowPlayingRow(
        track_id=tid or None,
        title=None,
        playlist_position=db.get_state_int(BotStateKey.PLAYLIST_POSITION, 0),
        playback_position_seconds=db.get_state_int(BotStateKey.PLAYBACK_POSITION_SECONDS, 0),
        is_paused=db.get_state_bool(BotStateKey.IS_PAUSED, False),
    )


def current_watchers(db: Database, *, now_iso: str | None = None) -> list[WatcherRow]:
    """Users with an open watch_session, with running duration."""
    rows = db.fetchall(
        """
        SELECT user_id, username, server_nickname, joined_at,
               CAST(strftime('%s', COALESCE(?, 'now')) - strftime('%s', joined_at) AS INTEGER)
                   AS seconds_so_far
        FROM watch_sessions
        WHERE left_at IS NULL
        ORDER BY joined_at ASC
        """,
        (now_iso,),
    )
    return [
        WatcherRow(
            user_id=r["user_id"],
            username=r["username"],
            server_nickname=r["server_nickname"],
            joined_at=r["joined_at"],
            seconds_so_far=max(0, int(r["seconds_so_far"] or 0)),
        )
        for r in rows
    ]


def leaderboard(db: Database, *, period: str = "alltime", limit: int = 100) -> list[LeaderboardRow]:
    """Return ranked users by total seconds.

    `period`: 'alltime' or 'monthly'. Any other value is treated as alltime.
    """
    col = "total_seconds_monthly" if period == "monthly" else "total_seconds_alltime"
    rows = db.fetchall(
        f"""
        SELECT user_id, username, server_nickname, {col} AS secs
        FROM user_totals
        WHERE {col} > 0
        ORDER BY {col} DESC, username ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        LeaderboardRow(
            rank=i,
            user_id=r["user_id"],
            username=r["username"],
            server_nickname=r["server_nickname"],
            seconds=int(r["secs"] or 0),
        )
        for i, r in enumerate(rows, start=1)
    ]


def format_hms(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return " ".join(parts) or "0s"
