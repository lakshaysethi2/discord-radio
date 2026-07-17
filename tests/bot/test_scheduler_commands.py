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

    async def test_hanging_command_times_out(self, db: Database) -> None:
        import asyncio

        async def handler(cmd: str, payload: dict | None) -> str:
            await asyncio.sleep(10)
            return "ok"

        s = Scheduler(db=db, tracker=SessionTracker(db), command_handler=handler)
        commands.enqueue(db, command="skip", requested_by=None)
        await s.drain_commands(per_command_timeout=0.05)
        row = commands.recent(db)[0]
        assert row.result is not None
        assert "timed out" in row.result


class TestApplyServerFromIdle:
    """Regression: an idle bot (zero live stations) must still process the
    `apply_server` command and build its first station. A naive early
    `if not stations: return` guard would bounce the command before it could
    create a station — so saving in the dashboard never made the bot join.
    """

    class _FakeChannel:
        def __init__(self, tid: int) -> None:
            self.text_channel_id = tid

    class _FakeStation:
        def __init__(self, gid: str, vid: str, tid: str) -> None:
            self.guild_id = gid
            self.voice_channel_id = vid
            self.text_channel_id = tid
            self.now_playing = TestApplyServerFromIdle._FakeChannel(int(tid))
            self.milestones = TestApplyServerFromIdle._FakeChannel(int(tid))

    async def test_apply_server_builds_first_station_from_empty(self, db: Database) -> None:
        from bot.main import apply_server_config
        from db import guilds as guilds_db

        # Seed an enabled config + matching channel ids, just like the
        # dashboard save would have written.
        guilds_db.discover_guild(db, "1", "Server One")
        guilds_db.apply_guild_config(
            db, "1", enabled=True, voice_channel_id="100", text_channel_id="200"
        )

        stations: dict[str, object] = {}
        announcers: dict[str, object] = {}
        built: list[tuple[object, object]] = []

        class _FakeGuild:
            def __init__(self, gid: str) -> None:
                self.id = int(gid)

            def get_channel(self, _cid):  # pragma: no cover — unused on apply path
                return object()

        async def build_station(guild, cfg):
            built.append((guild, cfg))
            return TestApplyServerFromIdle._FakeStation(
                cfg.guild_id, cfg.voice_channel_id, cfg.text_channel_id
            )

        async def teardown_station(station):  # pragma: no cover — not invoked here
            pass

        class _Client:
            def get_guild(self, gid: int):
                return _FakeGuild("1")

        # Mirror the real handler's ordering: apply_server runs *before* any
        # "no stations" guard, so it can create the first station.
        async def handler(command: str, payload: dict | None) -> str:
            if command == "apply_server":
                gid = (payload or {}).get("guild_id")
                if not gid:
                    return "error: apply_server requires payload {guild_id}"
                return await apply_server_config(
                    db=db,
                    client=_Client(),
                    stations=stations,
                    per_guild_announcers=announcers,
                    build_station=build_station,
                    teardown_station=teardown_station,
                    guild_id=gid,
                )
            return "ok"

        scheduler = Scheduler(db=db, tracker=SessionTracker(db), command_handler=handler)

        # Bot is idle: no stations yet.
        assert stations == {}

        commands.enqueue(db, command="apply_server", requested_by="42", payload={"guild_id": "1"})
        n = await scheduler.drain_commands()

        assert n == 1
        assert built, "apply_server should have invoked build_station"
        assert "1" in stations, "first station must be registered"
        # Consumed + marked done.
        assert commands.pending(db) == []
        assert commands.recent(db, limit=1)[0].result == "ok:applied"
