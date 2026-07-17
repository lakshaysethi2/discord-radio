"""Session tracking tests — the trickiest logic in the bot."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.tracker import SessionTracker, month_key_for
from db.database import Database

UTC = UTC


def dt(y=2024, m=6, d=1, h=12, mi=0, s=0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


@pytest.fixture
def tracker(db: Database) -> SessionTracker:
    return SessionTracker(db, min_session_seconds=30)


# ------------------------------------------------------------------ open/close
class TestOpenClose:
    def test_open_creates_row(self, tracker: SessionTracker, db: Database) -> None:
        sid = tracker.open_session(
            user_id="u1",
            username="Alice",
            server_nickname="A",
            track_id="t1",
            now=dt(),
        )
        row = db.fetchone("SELECT * FROM watch_sessions WHERE session_id=?", (sid,))
        assert row["user_id"] == "u1"
        assert row["left_at"] is None
        assert row["is_complete"] == 0

    def test_close_short_session_dropped(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1",
            username="A",
            server_nickname=None,
            track_id="t1",
            now=dt(h=12, mi=0, s=0),
        )
        closed = tracker.close_session(user_id="u1", now=dt(h=12, mi=0, s=10))
        assert closed is not None
        assert closed.was_counted is False
        # Row is gone.
        assert db.fetchone("SELECT 1 FROM watch_sessions WHERE user_id='u1'") is None
        assert db.fetchone("SELECT 1 FROM user_totals WHERE user_id='u1'") is None

    def test_close_long_session_credits_time(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1",
            username="A",
            server_nickname="Ali",
            track_id="t1",
            now=dt(h=12),
        )
        closed = tracker.close_session(user_id="u1", now=dt(h=13))
        assert closed is not None
        assert closed.was_counted is True
        assert closed.duration_seconds == 3600
        row = db.fetchone("SELECT * FROM user_totals WHERE user_id='u1'")
        assert row["total_seconds_alltime"] == 3600
        assert row["total_seconds_monthly"] == 3600

    def test_close_returns_none_if_no_open(self, tracker: SessionTracker) -> None:
        assert tracker.close_session(user_id="ghost") is None

    def test_duplicate_open_closes_previous(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=10)
        )
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t2", now=dt(h=11)
        )
        # Only one open session at any time.
        rows = db.fetchall("SELECT * FROM watch_sessions WHERE user_id='u1' AND left_at IS NULL")
        assert len(rows) == 1


# ------------------------------------------------------------------- checkpoint
class TestCheckpoint:
    def test_checkpoint_credits_partial_time(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=12)
        )
        n = tracker.checkpoint_open_sessions(now=dt(h=13))
        assert n == 1
        row = db.fetchone("SELECT total_seconds_alltime FROM user_totals WHERE user_id='u1'")
        assert row["total_seconds_alltime"] == 3600

    def test_close_after_checkpoint_no_double_count(
        self, tracker: SessionTracker, db: Database
    ) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=12)
        )
        tracker.checkpoint_open_sessions(now=dt(h=13))  # +1h credited
        tracker.close_session(user_id="u1", now=dt(h=13, mi=30))  # +30m more
        row = db.fetchone("SELECT total_seconds_alltime FROM user_totals WHERE user_id='u1'")
        assert row["total_seconds_alltime"] == 3600 + 1800

    def test_two_checkpoints(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=12)
        )
        tracker.checkpoint_open_sessions(now=dt(h=13))
        tracker.checkpoint_open_sessions(now=dt(h=14))
        row = db.fetchone("SELECT total_seconds_alltime FROM user_totals WHERE user_id='u1'")
        assert row["total_seconds_alltime"] == 7200

    def test_checkpoint_no_open_sessions(self, tracker: SessionTracker) -> None:
        assert tracker.checkpoint_open_sessions(now=dt()) == 0


# ----------------------------------------------------------- crash recovery
class TestOrphans:
    def test_close_orphans_uses_last_checkpoint(
        self, tracker: SessionTracker, db: Database
    ) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=12)
        )
        tracker.checkpoint_open_sessions(now=dt(h=13))  # credit 1h, checkpointed_at=13:00
        # Simulate a crash — bot restarts hours later.
        n = tracker.close_orphan_sessions(now=dt(h=20))
        assert n == 1
        row = db.fetchone("SELECT * FROM watch_sessions WHERE user_id='u1'")
        # We should NOT have credited 8 hours — only the hour already given.
        # left_at should be the checkpointed_at (13:00), not now (20:00).
        assert row["left_at"] == "2024-06-01 13:00:00"
        assert row["duration_seconds"] == 3600
        totals = db.fetchone("SELECT total_seconds_alltime FROM user_totals WHERE user_id='u1'")
        assert totals["total_seconds_alltime"] == 3600  # unchanged by orphan close

    def test_close_orphans_no_checkpoint(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=12)
        )
        # Crash immediately after join, no checkpoint credited.
        tracker.close_orphan_sessions(now=dt(h=20))
        row = db.fetchone("SELECT * FROM watch_sessions WHERE user_id='u1'")
        # left_at = joined_at (no time elapsed we can vouch for) → duration 0.
        assert row["duration_seconds"] == 0

    def test_orphan_close_idempotent(self, tracker: SessionTracker) -> None:
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt()
        )
        assert tracker.close_orphan_sessions(now=dt(h=13)) == 1
        # Second call has nothing to do.
        assert tracker.close_orphan_sessions(now=dt(h=14)) == 0


# ----------------------------------------------------------- month rollover
class TestMonthKey:
    def test_month_key_format(self) -> None:
        assert month_key_for(dt(y=2024, m=1)) == "2024-01"
        assert month_key_for(dt(y=2024, m=12)) == "2024-12"

    def test_close_over_month_boundary_resets_monthly(
        self, tracker: SessionTracker, db: Database
    ) -> None:
        # Session credit in October.
        tracker.open_session(
            user_id="u1",
            username="A",
            server_nickname=None,
            track_id="t1",
            now=dt(y=2024, m=10, d=1, h=0),
        )
        tracker.close_session(user_id="u1", now=dt(y=2024, m=10, d=1, h=1))
        assert (
            db.fetchone("SELECT month_key FROM user_totals WHERE user_id='u1'")["month_key"]
            == "2024-10"
        )

        # Fresh session in November — the monthly counter should reset to the new session's duration.
        tracker.open_session(
            user_id="u1",
            username="A",
            server_nickname=None,
            track_id="t1",
            now=dt(y=2024, m=11, d=1, h=0),
        )
        tracker.close_session(user_id="u1", now=dt(y=2024, m=11, d=1, h=2))
        row = db.fetchone("SELECT * FROM user_totals WHERE user_id='u1'")
        assert row["month_key"] == "2024-11"
        assert row["total_seconds_monthly"] == 7200  # only the November session
        assert row["total_seconds_alltime"] == 3600 + 7200


# ----------------------------------------------------------- identity refresh
class TestIdentityRefresh:
    def test_close_refreshes_username(self, tracker: SessionTracker, db: Database) -> None:
        tracker.open_session(
            user_id="u1", username="OldName", server_nickname="Old", track_id="t1", now=dt(h=12)
        )
        # Simulate a rename before close.
        tracker.close_session(user_id="u1", now=dt(h=13))
        # Now they rejoin with new name.
        tracker.open_session(
            user_id="u1", username="NewName", server_nickname="New", track_id="t1", now=dt(h=14)
        )
        tracker.close_session(user_id="u1", now=dt(h=15))
        row = db.fetchone("SELECT username, server_nickname FROM user_totals WHERE user_id='u1'")
        assert row["username"] == "NewName"
        assert row["server_nickname"] == "New"
