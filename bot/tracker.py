"""Voice-channel session tracking (blueprint §6).

`SessionTracker` is pure — no discord.py imports. It gets called from
`bot.main`'s `on_voice_state_update` handler with plain user data. This makes
it easy to unit-test all the interesting edge cases:

* Short session (< MIN_SESSION_SECONDS) is dropped entirely.
* Long session persists duration + updates `user_totals` atomically.
* Hourly checkpoint credits partial time without closing the session.
* Startup recovery: any `left_at IS NULL` rows from a prior crash get closed
  now, using their `checkpointed_at` (or `joined_at`) as `left_at` — so we
  never over-count during a crash but we also don't lose already-credited time.
* Month rollover: if a user's `month_key` differs from the current month, we
  archive their monthly total (implicitly, by the monthly scheduler) then
  reset — but at write time we always keep month_key up-to-date.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from db.database import Database
from db.models import MILESTONES

log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """SQLite-friendly ISO-8601, always UTC, no timezone suffix (SQLite chokes on tz)."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def month_key_for(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


@dataclass(slots=True)
class ClosedSession:
    """Return value of `close_session` — enough info for milestones to decide."""

    session_id: int
    user_id: str
    duration_seconds: int
    was_counted: bool  # False if it was shorter than MIN_SESSION_SECONDS


class SessionTracker:
    def __init__(self, db: Database, *, min_session_seconds: int = 30) -> None:
        self.db = db
        self.min_session_seconds = min_session_seconds

    # -------------------------------------------------------- open / close
    def open_session(
        self,
        *,
        user_id: str,
        username: str,
        server_nickname: str | None,
        track_id: str | None,
        now: datetime | None = None,
        guild_id: str = "",
    ) -> int:
        """Create a new open watch_session row. Returns session_id.

        ``guild_id`` scopes the session to a particular Discord server so two
        servers can credit the same person independently and report their own
        watcher counts. It defaults to ``""`` (legacy / unspecified) so
        single-guild callers don't have to change.
        """
        joined_at = _iso(now or _now_utc())
        # If the user already has an open session (double-join weirdness),
        # close the stale one at duration 0 first so we never have two rows
        # with left_at NULL for the same (user, guild).
        stale = self.db.fetchone(
            "SELECT session_id FROM watch_sessions "
            "WHERE user_id=? AND left_at IS NULL AND guild_id=?",
            (user_id, guild_id),
        )
        if stale is not None:
            self.db.execute(
                "UPDATE watch_sessions SET left_at=?, duration_seconds=0, is_complete=1 "
                "WHERE session_id=?",
                (joined_at, stale["session_id"]),
            )
            log.warning("closed stale open session for user %s (guild %s)", user_id, guild_id)

        cur = self.db.execute(
            "INSERT INTO watch_sessions"
            "(user_id, username, server_nickname, track_id, joined_at, guild_id) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, username, server_nickname, track_id, joined_at, guild_id),
        )
        return int(cur.lastrowid)

    def close_session(
        self, *, user_id: str, now: datetime | None = None, guild_id: str = ""
    ) -> ClosedSession | None:
        """Close the (single) open session for `user_id` in `guild_id`.

        Returns None if there was no open session (e.g. bot was down when they
        joined) or if the session is shorter than MIN_SESSION_SECONDS (in
        which case the row is deleted).
        """
        end = now or _now_utc()
        row = self.db.fetchone(
            "SELECT * FROM watch_sessions WHERE user_id=? AND left_at IS NULL AND guild_id=? "
            "ORDER BY session_id DESC LIMIT 1",
            (user_id, guild_id),
        )
        if row is None:
            return None

        joined_at = _parse_iso(row["joined_at"])
        checkpointed_at = _parse_iso(row["checkpointed_at"]) if row["checkpointed_at"] else None
        duration = int((end - joined_at).total_seconds())
        if duration < 0:
            duration = 0

        if duration < self.min_session_seconds:
            # Blueprint: "Delete the session record entirely (don't count it)".
            self.db.execute("DELETE FROM watch_sessions WHERE session_id=?", (row["session_id"],))
            log.info(
                "session %s (user %s) below threshold %ds — dropped",
                row["session_id"],
                user_id,
                self.min_session_seconds,
            )
            return ClosedSession(
                session_id=int(row["session_id"]),
                user_id=user_id,
                duration_seconds=duration,
                was_counted=False,
            )

        # Credit only the portion *not already credited* by hourly checkpoints.
        already_credited_end = checkpointed_at or joined_at
        incremental = int((end - already_credited_end).total_seconds())
        if incremental < 0:
            incremental = 0

        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE watch_sessions SET left_at=?, duration_seconds=?, is_complete=1 "
                "WHERE session_id=?",
                (_iso(end), duration, row["session_id"]),
            )
            _add_user_time(
                cur,
                user_id=user_id,
                username=row["username"],
                server_nickname=row["server_nickname"],
                seconds=incremental,
                now=end,
            )
        return ClosedSession(
            session_id=int(row["session_id"]),
            user_id=user_id,
            duration_seconds=duration,
            was_counted=True,
        )

    # -------------------------------------------------------- checkpoints
    def checkpoint_open_sessions(self, now: datetime | None = None) -> int:
        """Hourly: credit partial time for every open session. Returns count."""
        end = now or _now_utc()
        rows = self.db.fetchall("SELECT * FROM watch_sessions WHERE left_at IS NULL")
        n = 0
        for row in rows:
            joined_at = _parse_iso(row["joined_at"])
            checkpointed_at = (
                _parse_iso(row["checkpointed_at"]) if row["checkpointed_at"] else joined_at
            )
            partial = int((end - checkpointed_at).total_seconds())
            if partial <= 0:
                continue
            with self.db.transaction() as cur:
                cur.execute(
                    "UPDATE watch_sessions SET checkpointed_at=? WHERE session_id=?",
                    (_iso(end), row["session_id"]),
                )
                _add_user_time(
                    cur,
                    user_id=row["user_id"],
                    username=row["username"],
                    server_nickname=row["server_nickname"],
                    seconds=partial,
                    now=end,
                )
            n += 1
        return n

    # ----------------------------------------------------- crash recovery
    def close_orphan_sessions(self, now: datetime | None = None) -> int:
        """On startup, close any sessions left open by a previous run.

        We treat the *last* time we credited the user (checkpointed_at or
        joined_at) as `left_at` — that means we never over-count time the
        bot wasn't actually watching. Users lose at most one checkpoint
        interval of watch time in the worst crash.
        """
        end = now or _now_utc()
        rows = self.db.fetchall("SELECT * FROM watch_sessions WHERE left_at IS NULL")
        for row in rows:
            joined_at = _parse_iso(row["joined_at"])
            checkpointed_at = (
                _parse_iso(row["checkpointed_at"]) if row["checkpointed_at"] else joined_at
            )
            # Cap `left_at` at min(now, last-known-good) — never fabricate time.
            left_at = min(checkpointed_at, end)
            duration = int((left_at - joined_at).total_seconds())
            duration = max(0, duration)
            self.db.execute(
                "UPDATE watch_sessions SET left_at=?, duration_seconds=?, is_complete=1 "
                "WHERE session_id=?",
                (_iso(left_at), duration, row["session_id"]),
            )
        return len(rows)

    # ---- read helpers used by dashboard / milestones ---------------------
    def open_sessions(self) -> list:
        return self.db.fetchall(
            "SELECT * FROM watch_sessions WHERE left_at IS NULL ORDER BY joined_at ASC"
        )

    def user_alltime_seconds(self, user_id: str) -> int:
        row = self.db.fetchone(
            "SELECT total_seconds_alltime FROM user_totals WHERE user_id=?", (user_id,)
        )
        return int(row["total_seconds_alltime"]) if row else 0


# -----------------------------------------------------------------------------
# Helpers shared with the scheduler.
# -----------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """Parse an ISO string, always returning UTC-aware datetime."""
    # SQLite stores our writes as "YYYY-MM-DD HH:MM:SS"; be lenient though.
    s = s.replace("T", " ")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _add_user_time(
    cur,
    *,
    user_id: str,
    username: str,
    server_nickname: str | None,
    seconds: int,
    now: datetime,
) -> None:
    """Idempotent user_totals upsert with monthly-key handling.

    * All-time counter: increment.
    * Monthly counter: increment IF row's `month_key` matches current month.
      If it's stale, RESET to `seconds` and update `month_key`.
    """
    if seconds <= 0:
        # Still refresh the username/nickname while we're here so the dashboard
        # shows the freshest identity.
        cur.execute(
            "UPDATE user_totals SET username=?, server_nickname=?, last_updated=? WHERE user_id=?",
            (username, server_nickname, _iso(now), user_id),
        )
        return

    mkey = month_key_for(now)
    now_iso = _iso(now)

    row = cur.execute("SELECT * FROM user_totals WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        # Set all milestone flags to 0 explicitly for clarity.
        cur.execute(
            "INSERT INTO user_totals(user_id, username, server_nickname, "
            "total_seconds_alltime, total_seconds_monthly, month_key, last_updated) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, username, server_nickname, seconds, seconds, mkey, now_iso),
        )
        return

    if row["month_key"] == mkey:
        new_monthly = int(row["total_seconds_monthly"] or 0) + seconds
    else:
        # Month has rolled over since this user's last update.
        new_monthly = seconds
    new_alltime = int(row["total_seconds_alltime"] or 0) + seconds
    cur.execute(
        "UPDATE user_totals SET username=?, server_nickname=?, "
        "total_seconds_alltime=?, total_seconds_monthly=?, month_key=?, last_updated=? "
        "WHERE user_id=?",
        (
            username,
            server_nickname,
            new_alltime,
            new_monthly,
            mkey,
            now_iso,
            user_id,
        ),
    )


# Re-exported for convenience in tests/other modules.
__all__ = [
    "MILESTONES",  # forwarded from db.models for milestone module
    "ClosedSession",
    "SessionTracker",
    "month_key_for",
]
