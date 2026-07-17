from __future__ import annotations

from dashboard.queries import (
    current_watchers,
    format_hms,
    leaderboard,
    now_playing,
)
from db.database import Database
from db.models import BotStateKey


class TestNowPlaying:
    def test_defaults_when_empty(self, db: Database) -> None:
        np = now_playing(db)
        assert np.track_id is None
        assert np.playlist_position == 0
        assert np.is_paused is False

    def test_reads_state(self, db: Database) -> None:
        db.set_state(BotStateKey.CURRENT_TRACK_ID, "abc")
        db.set_state(BotStateKey.PLAYLIST_POSITION, 7)
        db.set_state(BotStateKey.PLAYBACK_POSITION_SECONDS, 120)
        db.set_state(BotStateKey.IS_PAUSED, True)
        np = now_playing(db)
        assert np.track_id == "abc"
        assert np.playlist_position == 7
        assert np.playback_position_seconds == 120
        assert np.is_paused is True


class TestCurrentWatchers:
    def test_open_session_appears(self, db: Database) -> None:
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, server_nickname, "
            "joined_at) VALUES(?,?,?,?)",
            ("u1", "Alice", "Ali", "2024-01-01 00:00:00"),
        )
        rows = current_watchers(db, now_iso="2024-01-01 00:00:30")
        assert len(rows) == 1
        assert rows[0].user_id == "u1"
        assert rows[0].seconds_so_far == 30

    def test_closed_session_hidden(self, db: Database) -> None:
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at, left_at, "
            "is_complete) VALUES(?,?,?,?,?)",
            ("u1", "A", "2024-01-01 00:00:00", "2024-01-01 00:10:00", 1),
        )
        assert current_watchers(db) == []


class TestLeaderboard:
    def test_ranked_by_seconds(self, db: Database) -> None:
        for uid, secs in [("u1", 100), ("u2", 500), ("u3", 300)]:
            db.execute(
                "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
                "total_seconds_monthly) VALUES(?,?,?,?)",
                (uid, f"U_{uid}", secs, secs // 2),
            )
        got = leaderboard(db, period="alltime")
        assert [r.user_id for r in got] == ["u2", "u3", "u1"]
        assert [r.rank for r in got] == [1, 2, 3]

    def test_monthly_uses_monthly_column(self, db: Database) -> None:
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
            "total_seconds_monthly) VALUES('u1', 'A', 10000, 5)",
        )
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
            "total_seconds_monthly) VALUES('u2', 'B', 100, 500)",
        )
        got = leaderboard(db, period="monthly")
        assert [r.user_id for r in got] == ["u2", "u1"]

    def test_zero_seconds_excluded(self, db: Database) -> None:
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
            "total_seconds_monthly) VALUES('u1', 'A', 0, 0)",
        )
        assert leaderboard(db) == []

    def test_limit(self, db: Database) -> None:
        for i in range(10):
            db.execute(
                "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
                "total_seconds_monthly) VALUES(?,?,?,0)",
                (f"u{i}", f"U{i}", 100 + i),
            )
        assert len(leaderboard(db, limit=3)) == 3

    def test_unknown_period_defaults_alltime(self, db: Database) -> None:
        # Reaches the same column as 'alltime'
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
            "total_seconds_monthly) VALUES('u1', 'A', 100, 0)",
        )
        got = leaderboard(db, period="not-a-real-period")
        assert got and got[0].user_id == "u1"


class TestFormatHms:
    def test_zero(self) -> None:
        assert format_hms(0) == "0s"

    def test_seconds_only(self) -> None:
        assert format_hms(45) == "45s"

    def test_minutes(self) -> None:
        assert format_hms(65) == "1m 5s"

    def test_hours(self) -> None:
        assert format_hms(3661) == "1h 1m"

    def test_full_hours_no_seconds(self) -> None:
        assert format_hms(7200) == "2h"

    def test_negative(self) -> None:
        assert format_hms(-1) == "0s"
