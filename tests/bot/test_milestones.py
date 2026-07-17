from __future__ import annotations

from bot.milestones import MilestoneChecker
from db.database import Database


def _seed(db: Database, user_id: str, alltime_seconds: int, **flags: int) -> None:
    db.execute(
        "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
        "total_seconds_monthly, month_key, "
        "milestone_5h, milestone_10h, milestone_100h, milestone_1000h) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            f"User_{user_id}",
            alltime_seconds,
            0,
            "2024-11",
            flags.get("m5", 0),
            flags.get("m10", 0),
            flags.get("m100", 0),
            flags.get("m1000", 0),
        ),
    )


class TestChecker:
    def test_unknown_user_no_milestones(self, db: Database) -> None:
        c = MilestoneChecker(db)
        assert c.check_user("ghost") == []

    def test_under_threshold_nothing(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=4 * 3600)
        c = MilestoneChecker(db)
        assert c.check_user("u1") == []

    def test_exactly_5h_triggers(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [5]
        # Flag flipped so re-check is empty.
        assert c.check_user("u1") == []

    def test_multiple_at_once(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=100 * 3600 + 5)  # crosses 5, 10, 100
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert sorted(m.hours for m in got) == [5, 10, 100]

    def test_existing_flags_respected(self, db: Database) -> None:
        # Already given 5h milestone previously.
        _seed(db, "u1", alltime_seconds=15 * 3600, m5=1)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [10]

    def test_1000h(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=1000 * 3600, m5=1, m10=1, m100=1)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [1000]

    def test_username_included(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert got[0].username == "User_u1"
        assert got[0].user_id == "u1"
