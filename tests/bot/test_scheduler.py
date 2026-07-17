"""Scheduler tests focus on the pure `run_monthly_reset` — the asyncio loops
are tested implicitly by the units they call."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.scheduler import monthly_reset_due, run_monthly_reset
from db.database import Database
from db.models import BotStateKey

UTC = UTC


def dt(y=2024, m=11, d=1, h=0, mi=0, s=0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def _seed(db: Database, user_id: str, monthly: int, mkey: str, alltime: int | None = None) -> None:
    if alltime is None:
        alltime = monthly
    db.execute(
        "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
        "total_seconds_monthly, month_key) VALUES(?,?,?,?,?)",
        (user_id, f"User_{user_id}", alltime, monthly, mkey),
    )


# --------------------------------------------------------------------- due
class TestDue:
    def test_due_on_fresh_db(self, db: Database) -> None:
        assert monthly_reset_due(db, now=dt()) is True

    def test_not_due_if_same_month_marked(self, db: Database) -> None:
        db.set_state(BotStateKey.LAST_MONTHLY_RESET, "2024-11")
        assert monthly_reset_due(db, now=dt(y=2024, m=11)) is False

    def test_due_after_month_change(self, db: Database) -> None:
        db.set_state(BotStateKey.LAST_MONTHLY_RESET, "2024-10")
        assert monthly_reset_due(db, now=dt(y=2024, m=11)) is True


# ------------------------------------------------------------------ reset
class TestReset:
    def test_no_users_still_marks_done(self, db: Database) -> None:
        summary = run_monthly_reset(db, now=dt())
        assert summary["ran"] is True
        assert db.get_state(BotStateKey.LAST_MONTHLY_RESET) == "2024-11"

    def test_snapshot_and_zero(self, db: Database) -> None:
        _seed(db, "u1", monthly=7200, mkey="2024-10")
        _seed(db, "u2", monthly=3600, mkey="2024-10")
        _seed(db, "u3", monthly=10, mkey="2024-10")  # will rank last
        run_monthly_reset(db, now=dt(y=2024, m=11))

        snaps = db.fetchall(
            "SELECT user_id, rank, total_seconds FROM monthly_snapshots "
            "WHERE month_key='2024-10' ORDER BY rank"
        )
        assert [s["user_id"] for s in snaps] == ["u1", "u2", "u3"]
        assert [s["rank"] for s in snaps] == [1, 2, 3]

        rows = db.fetchall("SELECT user_id, total_seconds_monthly, month_key FROM user_totals")
        for r in rows:
            assert r["total_seconds_monthly"] == 0
            assert r["month_key"] == "2024-11"

    def test_double_run_is_noop(self, db: Database) -> None:
        _seed(db, "u1", monthly=100, mkey="2024-10")
        first = run_monthly_reset(db, now=dt(y=2024, m=11))
        second = run_monthly_reset(db, now=dt(y=2024, m=11))
        assert first["ran"] is True
        assert second["ran"] is False

    def test_alltime_preserved(self, db: Database) -> None:
        _seed(db, "u1", monthly=100, mkey="2024-10", alltime=99999)
        run_monthly_reset(db, now=dt(y=2024, m=11))
        row = db.fetchone("SELECT total_seconds_alltime FROM user_totals WHERE user_id='u1'")
        assert row["total_seconds_alltime"] == 99999

    def test_upsert_snapshot_idempotent(self, db: Database) -> None:
        """Running reset for a month twice must not create duplicate snapshot rows."""
        _seed(db, "u1", monthly=100, mkey="2024-10")
        run_monthly_reset(db, now=dt(y=2024, m=11))
        # Simulate an operator manually re-running reset for the same target month.
        # This is prevented by the LAST_MONTHLY_RESET guard, but we double-check
        # the unique index prevents duplicates in case of manual DB edits.
        db.set_state(BotStateKey.LAST_MONTHLY_RESET, "2024-10")
        _seed(db, "u1_x", monthly=50, mkey="2024-10")  # would-be duplicate month
        run_monthly_reset(db, now=dt(y=2024, m=11))
        # Only one row per (user, month).
        rows = db.fetchall(
            "SELECT user_id FROM monthly_snapshots WHERE user_id='u1' AND month_key='2024-10'"
        )
        assert len(rows) == 1

    def test_skips_users_with_zero_monthly(self, db: Database) -> None:
        _seed(db, "u1", monthly=0, mkey="2024-10")
        _seed(db, "u2", monthly=100, mkey="2024-10")
        run_monthly_reset(db, now=dt(y=2024, m=11))
        snaps = db.fetchall("SELECT user_id FROM monthly_snapshots")
        assert [s["user_id"] for s in snaps] == ["u2"]

    def test_multi_month_snapshot(self, db: Database) -> None:
        """If some users are stuck on an even older month_key, each gets ranked separately."""
        _seed(db, "u1", monthly=500, mkey="2024-09")
        _seed(db, "u2", monthly=100, mkey="2024-10")
        run_monthly_reset(db, now=dt(y=2024, m=11))
        assert (
            db.fetchone(
                "SELECT rank FROM monthly_snapshots WHERE user_id='u1' AND month_key='2024-09'"
            )["rank"]
            == 1
        )
        assert (
            db.fetchone(
                "SELECT rank FROM monthly_snapshots WHERE user_id='u2' AND month_key='2024-10'"
            )["rank"]
            == 1
        )
