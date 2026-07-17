"""Scheduler milestone routing to the correct server (§servers)."""

from __future__ import annotations

from bot.scheduler import Scheduler
from bot.tracker import SessionTracker
from db.database import Database


class FakeAnnouncer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def check_and_announce(self, user_id: str):
        self.calls.append(user_id)
        return []


class TestPerGuildAnnounce:
    async def test_routes_to_guild_announcer(self, db: Database) -> None:
        tracker = SessionTracker(db)
        g1, g2 = FakeAnnouncer(), FakeAnnouncer()
        sched = Scheduler(db=db, tracker=tracker, per_guild_announcers={"g1": g1, "g2": g2})
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t", guild_id="g1"
        )
        tracker.open_session(
            user_id="u2", username="B", server_nickname=None, track_id="t", guild_id="g2"
        )
        for row in tracker.open_sessions():
            await sched._maybe_announce(row["user_id"], row["guild_id"])
        assert g1.calls == ["u1"]
        assert g2.calls == ["u2"]

    async def test_falls_back_to_single_milestones(self, db: Database) -> None:
        tracker = SessionTracker(db)
        single = FakeAnnouncer()
        sched = Scheduler(db=db, tracker=tracker, milestones=single)
        tracker.open_session(
            user_id="u1", username="A", server_nickname=None, track_id="t", guild_id="g1"
        )
        await sched._maybe_announce("u1", "g1")
        assert single.calls == ["u1"]

    async def test_unknown_guild_with_no_fallback_does_nothing(self, db: Database) -> None:
        tracker = SessionTracker(db)
        sched = Scheduler(db=db, tracker=tracker, per_guild_announcers={})
        # No announcer + no fallback → safe no-op, no raise.
        await sched._maybe_announce("u1", "ghost")
