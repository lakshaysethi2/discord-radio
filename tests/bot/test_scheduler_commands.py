from __future__ import annotations

from bot.scheduler import Scheduler
from bot.tracker import SessionTracker
from dashboard import commands
from db.database import Database


class TestDrainCommands:
    async def test_drain_marks_done(self, db: Database) -> None:
        seen: list[tuple[str, dict | None]] = []

        async def handler(cmd: str, payload: dict | None) -> str:
            seen.append((cmd, payload))
            return "ok:test"

        s = Scheduler(
            db=db,
            tracker=SessionTracker(db),
            command_handler=handler,
        )

        commands.enqueue(db, command="skip", requested_by="42")
        commands.enqueue(db, command="pause", requested_by="42")

        n = await s.drain_commands()
        assert n == 2
        assert seen == [("skip", None), ("pause", None)]
        # Both are marked done.
        assert commands.pending(db) == []
        rec = commands.recent(db, limit=5)
        for r in rec:
            assert r.result == "ok:test"

    async def test_drain_handler_exception_marks_error(self, db: Database) -> None:
        async def handler(cmd: str, payload: dict | None) -> str:
            raise RuntimeError("boom")

        s = Scheduler(db=db, tracker=SessionTracker(db), command_handler=handler)

        commands.enqueue(db, command="skip", requested_by=None)
        await s.drain_commands()

        row = commands.recent(db)[0]
        assert row.executed_at is not None
        assert row.result.startswith("error:")

    async def test_drain_no_handler_returns_zero(self, db: Database) -> None:
        s = Scheduler(db=db, tracker=SessionTracker(db), command_handler=None)
        commands.enqueue(db, command="skip", requested_by=None)
        assert await s.drain_commands() == 0
        # Still pending because we have no handler.
        assert len(commands.pending(db)) == 1

    async def test_drain_empty(self, db: Database) -> None:
        async def handler(cmd: str, payload: dict | None) -> str:
            return "ok"

        s = Scheduler(db=db, tracker=SessionTracker(db), command_handler=handler)
        assert await s.drain_commands() == 0

    async def test_payload_passed_through(self, db: Database) -> None:
        seen: list = []

        async def handler(cmd: str, payload: dict | None) -> str:
            seen.append(payload)
            return "ok"

        s = Scheduler(db=db, tracker=SessionTracker(db), command_handler=handler)
        commands.enqueue(db, command="skip", requested_by=None, payload={"reason": "test"})
        await s.drain_commands()
        assert seen == [{"reason": "test"}]
