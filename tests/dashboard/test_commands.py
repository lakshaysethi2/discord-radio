from __future__ import annotations

import pytest

from dashboard import commands
from db.database import Database


class TestEnqueue:
    def test_valid(self, db: Database) -> None:
        cid = commands.enqueue(db, command="skip", requested_by="42")
        assert cid > 0
        pend = commands.pending(db)
        assert len(pend) == 1
        assert pend[0].command == "skip"
        assert pend[0].requested_by == "42"
        assert pend[0].executed_at is None

    def test_invalid_rejected(self, db: Database) -> None:
        with pytest.raises(commands.UnknownCommandError):
            commands.enqueue(db, command="drop_database", requested_by="42")

    def test_payload_roundtrip(self, db: Database) -> None:
        commands.enqueue(db, command="skip", requested_by="42", payload={"reason": "test"})
        pend = commands.pending(db)
        assert pend[0].payload == {"reason": "test"}


class TestPending:
    def test_ordered_by_id(self, db: Database) -> None:
        for c in ("skip", "pause", "resume"):
            commands.enqueue(db, command=c, requested_by=None)
        got = [r.command for r in commands.pending(db)]
        assert got == ["skip", "pause", "resume"]

    def test_excluded_after_mark_done(self, db: Database) -> None:
        cid = commands.enqueue(db, command="pause", requested_by=None)
        commands.mark_done(db, cid, result="ok")
        assert commands.pending(db) == []


class TestRecent:
    def test_orders_desc(self, db: Database) -> None:
        ids = [commands.enqueue(db, command="skip", requested_by=None) for _ in range(3)]
        recent_ids = [r.command_id for r in commands.recent(db, limit=3)]
        assert recent_ids == list(reversed(ids))


class TestMarkDone:
    def test_sets_result(self, db: Database) -> None:
        cid = commands.enqueue(db, command="skip", requested_by=None)
        commands.mark_done(db, cid, result="ok:advanced")
        row = commands.recent(db)[0]
        assert row.executed_at is not None
        assert row.result == "ok:advanced"

    def test_no_such_id_is_silent(self, db: Database) -> None:
        # Should not raise.
        commands.mark_done(db, 999_999)
