"""Table DDLs + typed row dataclasses matching the blueprint §5.

Nothing here does I/O — the DDLs are executed by `db.database.Database.migrate()`
and the dataclasses are lightweight row containers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# -----------------------------------------------------------------------------
# DDL statements — kept in a stable, ordered list so migrations are deterministic.
# All statements are `CREATE ... IF NOT EXISTS` so migrate() is idempotent.
# -----------------------------------------------------------------------------

SCHEMA: tuple[str, ...] = (
    # ---- tracks ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tracks (
        track_id          TEXT PRIMARY KEY,
        title             TEXT NOT NULL,
        duration_seconds  INTEGER,
        playlist_position INTEGER UNIQUE,
        added_at          DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ---- watch_sessions ----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS watch_sessions (
        session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          TEXT NOT NULL,
        username         TEXT NOT NULL,
        server_nickname  TEXT,
        track_id         TEXT,
        joined_at        DATETIME NOT NULL,
        left_at          DATETIME,
        duration_seconds INTEGER,
        checkpointed_at  DATETIME,
        is_complete      BOOLEAN DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_watch_sessions_user      ON watch_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_watch_sessions_open      ON watch_sessions(user_id, left_at) WHERE left_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_watch_sessions_joined_at ON watch_sessions(joined_at)",
    # ---- user_totals -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS user_totals (
        user_id               TEXT PRIMARY KEY,
        username              TEXT NOT NULL,
        server_nickname       TEXT,
        total_seconds_alltime INTEGER DEFAULT 0,
        total_seconds_monthly INTEGER DEFAULT 0,
        month_key             TEXT,
        last_updated          DATETIME,
        milestone_5h          BOOLEAN DEFAULT 0,
        milestone_10h         BOOLEAN DEFAULT 0,
        milestone_100h        BOOLEAN DEFAULT 0,
        milestone_1000h       BOOLEAN DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_totals_alltime ON user_totals(total_seconds_alltime DESC)",
    "CREATE INDEX IF NOT EXISTS idx_user_totals_monthly ON user_totals(total_seconds_monthly DESC)",
    # ---- bot_state ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # ---- monthly_snapshots -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS monthly_snapshots (
        snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       TEXT NOT NULL,
        username      TEXT NOT NULL,
        month_key     TEXT NOT NULL,
        total_seconds INTEGER DEFAULT 0,
        rank          INTEGER,
        snapshot_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_snapshots_user_month ON monthly_snapshots(user_id, month_key)",
    "CREATE INDEX IF NOT EXISTS idx_monthly_snapshots_month ON monthly_snapshots(month_key)",
    # ---- dashboard_commands (control-plane queue, phase 9) -----------------
    # Dashboard writes intent, bot polls & executes. Keeps everything in one
    # SQLite file so we don't have to run an internal HTTP server in the bot.
    """
    CREATE TABLE IF NOT EXISTS dashboard_commands (
        command_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        command      TEXT NOT NULL,
        payload      TEXT,
        requested_by TEXT,
        requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        executed_at  DATETIME,
        result       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dashboard_commands_pending ON dashboard_commands(executed_at) WHERE executed_at IS NULL",
    # ---- guild_configs (multi-server management; §servers) ----------------
    # One row per Discord server the bot belongs to. Admins toggle `enabled`
    # and pick the voice/text channels from the dashboard. The bot discovers
    # rows here on startup and joins every *enabled* guild that has both
    # channel ids populated.
    """
    CREATE TABLE IF NOT EXISTS guild_configs (
        guild_id          TEXT PRIMARY KEY,
        guild_name        TEXT,
        enabled           BOOLEAN DEFAULT 0,
        voice_channel_id  TEXT,
        text_channel_id   TEXT,
        updated_at        DATETIME
    )
    """,
    # ---- guild_channels (cached channel lists for dashboard dropdowns) -----
    # Refreshed from Discord on every `on_ready` so the dashboard can render
    # <select> dropdowns without calling Discord itself.
    """
    CREATE TABLE IF NOT EXISTS guild_channels (
        guild_id     TEXT NOT NULL,
        channel_id   TEXT NOT NULL,
        channel_name TEXT,
        channel_type TEXT,  -- 'voice' | 'text'
        parent_id    TEXT,  -- for a text chat nested under a voice channel, the voice channel's id
        PRIMARY KEY (guild_id, channel_id)
    )
    """,
)


# -----------------------------------------------------------------------------
# Row dataclasses. Kept intentionally slim — they mirror table columns 1:1.
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class Track:
    track_id: str
    title: str
    duration_seconds: int | None = None
    playlist_position: int | None = None
    added_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Track:
        return cls(
            track_id=row["track_id"],
            title=row["title"],
            duration_seconds=row["duration_seconds"],
            playlist_position=row["playlist_position"],
            added_at=row["added_at"],
        )


@dataclass(slots=True)
class WatchSession:
    session_id: int | None
    user_id: str
    username: str
    joined_at: str
    server_nickname: str | None = None
    track_id: str | None = None
    left_at: str | None = None
    duration_seconds: int | None = None
    checkpointed_at: str | None = None
    is_complete: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WatchSession:
        return cls(
            session_id=row["session_id"],
            user_id=row["user_id"],
            username=row["username"],
            server_nickname=row["server_nickname"],
            track_id=row["track_id"],
            joined_at=row["joined_at"],
            left_at=row["left_at"],
            duration_seconds=row["duration_seconds"],
            checkpointed_at=row["checkpointed_at"],
            is_complete=bool(row["is_complete"]),
        )


@dataclass(slots=True)
class UserTotals:
    user_id: str
    username: str
    server_nickname: str | None = None
    total_seconds_alltime: int = 0
    total_seconds_monthly: int = 0
    month_key: str | None = None
    last_updated: str | None = None
    milestone_5h: bool = False
    milestone_10h: bool = False
    milestone_100h: bool = False
    milestone_1000h: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> UserTotals:
        return cls(
            user_id=row["user_id"],
            username=row["username"],
            server_nickname=row["server_nickname"],
            total_seconds_alltime=row["total_seconds_alltime"] or 0,
            total_seconds_monthly=row["total_seconds_monthly"] or 0,
            month_key=row["month_key"],
            last_updated=row["last_updated"],
            milestone_5h=bool(row["milestone_5h"]),
            milestone_10h=bool(row["milestone_10h"]),
            milestone_100h=bool(row["milestone_100h"]),
            milestone_1000h=bool(row["milestone_1000h"]),
        )


@dataclass(slots=True)
class MonthlySnapshot:
    snapshot_id: int | None
    user_id: str
    username: str
    month_key: str
    total_seconds: int = 0
    rank: int | None = None
    snapshot_at: str | None = None


@dataclass(slots=True)
class DashboardCommand:
    command_id: int | None
    command: str
    payload: str | None = None
    requested_by: str | None = None
    requested_at: str | None = None
    executed_at: str | None = None
    result: str | None = None


# Milestone thresholds — in hours. Column names must match §5.
MILESTONES: tuple[tuple[int, str], ...] = (
    (5, "milestone_5h"),
    (10, "milestone_10h"),
    (100, "milestone_100h"),
    (1000, "milestone_1000h"),
)


# bot_state keys — collected here for reference & to avoid typos elsewhere.
class BotStateKey:
    CURRENT_TRACK_ID = "current_track_id"
    PLAYBACK_POSITION_SECONDS = "playback_position_seconds"
    IS_PAUSED = "is_paused"
    NOW_PLAYING_MESSAGE_ID = "now_playing_message_id"
    PLAYLIST_POSITION = "playlist_position"
    LAST_MONTHLY_RESET = "last_monthly_reset"  # yyyy-mm we last snapshotted
    STREAM_VOLUME_PERCENT = "stream_volume_percent"
    ARCHIVE_ORG_ITEMS = "archive_org_items"


BOT_STATE_KEYS: frozenset[str] = frozenset(
    v for k, v in vars(BotStateKey).items() if not k.startswith("_") and isinstance(v, str)
)
