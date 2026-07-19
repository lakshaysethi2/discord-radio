"""Tests for bot.commands — slash command callbacks.

Uses fake/mock dependencies so no live Discord connection is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.commands import _fmt_duration, _watcher_count, build_commands
from bot.state import BotState
from db.database import Database
from provider.client import TrackResponse


# ---------------------------------------------------------------------------
# Fake / stub helpers
# ---------------------------------------------------------------------------

def _make_track(**kw) -> TrackResponse:
    defaults = {
        "track_id": "abc123",
        "title": "Test Track",
        "duration_seconds": 234,
        "local_path": "/cache/test.mp3",
        "provider_used": "local",
        "playlist_position": 3,
        "ready": True,
    }
    defaults.update(kw)
    return TrackResponse(**defaults)


class FakeRadioClock:
    """Minimal stub matching RadioClock's .position() and .is_playing()."""

    def __init__(self, position: float = 42.0, playing: bool = True) -> None:
        self._pos = position
        self._playing = playing

    def position(self) -> float:
        return self._pos

    def is_playing(self) -> bool:
        return self._playing


class FakeProvider:
    """Async stub that returns a pre-configured track from .get_by_id()."""

    def __init__(self, track: TrackResponse | None = None, exc: Exception | None = None) -> None:
        self.track = track
        self.exc = exc

    async def get_by_id(self, track_id: str) -> TrackResponse:
        if self.exc:
            raise self.exc
        if self.track is None:
            raise RuntimeError("no track configured")
        return self.track


def _fake_interaction(
    *,
    guild_id: int | None = 999,
    response_send: AsyncMock | None = None,
) -> MagicMock:
    """Build a MagicMock that behaves enough like discord.Interaction."""
    inter = MagicMock()
    inter.guild_id = guild_id
    inter.response = MagicMock()
    inter.response.send_message = response_send or AsyncMock()
    return inter


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------

class TestFmtDuration:
    def test_zero(self) -> None:
        assert _fmt_duration(0) == "—"

    def test_seconds_only(self) -> None:
        assert _fmt_duration(45) == "0m 45s"

    def test_minutes_seconds(self) -> None:
        assert _fmt_duration(125) == "2m 05s"

    def test_hours_minutes(self) -> None:
        assert _fmt_duration(3661) == "1h 01m"

    def test_negative(self) -> None:
        assert _fmt_duration(-5) == "—"


# ---------------------------------------------------------------------------
# _watcher_count
# ---------------------------------------------------------------------------

class TestWatcherCount:
    def test_no_sessions(self, db: Database) -> None:
        assert _watcher_count(db, "123") == 0

    def test_counts_scoped_to_guild(self, db: Database) -> None:
        # Insert two open sessions for different guilds.
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at, guild_id) "
            "VALUES(?,?,datetime('now'),?)",
            ("u1", "Alice", "guild-a"),
        )
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at, guild_id) "
            "VALUES(?,?,datetime('now'),?)",
            ("u2", "Bob", "guild-b"),
        )
        assert _watcher_count(db, "guild-a") == 1
        assert _watcher_count(db, "guild-b") == 1
        assert _watcher_count(db, "") == 2  # unscoped counts all

    def test_ignores_closed_sessions(self, db: Database) -> None:
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at, left_at, guild_id) "
            "VALUES(?,?,datetime('now'),datetime('now'),?)",
            ("u1", "Alice", "guild-a"),
        )
        assert _watcher_count(db, "guild-a") == 0


# ---------------------------------------------------------------------------
# build_commands
# ---------------------------------------------------------------------------

class TestBuildCommands:
    def test_returns_three_commands(self, db: Database, state: BotState) -> None:
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),  # type: ignore[arg-type]
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        names = [name for name, _, _ in cmds]
        assert "current" in names
        assert "next" in names
        assert "leaderboard" in names
        assert len(names) == 3


# ---------------------------------------------------------------------------
# /current
# ---------------------------------------------------------------------------

class TestCurrentCommand:
    @pytest.fixture
    def provider(self) -> FakeProvider:
        return FakeProvider(track=_make_track())

    @pytest.fixture
    def radio(self) -> FakeRadioClock:
        return FakeRadioClock(position=60.0, playing=True)

    def _get_current(self, cmds):
        for _, _, cb in cmds:
            if cb.__name__ == "current_command":
                return cb
        raise LookupError("current_command not found")

    async def test_shows_nothing_when_no_track(self, db: Database, state: BotState) -> None:
        state.current_track_id = None
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        cb = self._get_current(cmds)
        inter = _fake_interaction()
        await cb(inter)
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.call_args
        assert "Nothing is playing" in str(args[0])

    async def test_shows_current_track_info(
        self, db: Database, state: BotState, provider: FakeProvider, radio: FakeRadioClock
    ) -> None:
        state.current_track_id = "abc123"
        cmds = build_commands(
            db=db,
            provider=provider,
            state=state,
            radio=radio,
            stations={},
        )
        cb = self._get_current(cmds)
        inter = _fake_interaction()
        await cb(inter)
        inter.response.send_message.assert_awaited_once()
        _, kwargs = inter.response.send_message.call_args
        embed = kwargs["embed"]
        assert embed.title == "🎙️ Now Playing"
        assert "Test Track" in (embed.description or "")

    async def test_shows_paused_when_paused(
        self, db: Database, state: BotState, provider: FakeProvider, radio: FakeRadioClock
    ) -> None:
        state.current_track_id = "abc123"
        state.is_paused = True
        cmds = build_commands(
            db=db,
            provider=provider,
            state=state,
            radio=radio,
            stations={},
        )
        cb = self._get_current(cmds)
        inter = _fake_interaction()
        await cb(inter)
        _, kwargs = inter.response.send_message.call_args
        embed = kwargs["embed"]
        progress_field = next((f for f in embed.fields if f.name == "Progress"), None)
        assert progress_field is not None
        assert "⏸️" in progress_field.value

    async def test_handles_provider_error(
        self, db: Database, state: BotState, radio: FakeRadioClock
    ) -> None:
        state.current_track_id = "abc123"
        provider = FakeProvider(exc=RuntimeError("provider down"))
        cmds = build_commands(
            db=db,
            provider=provider,
            state=state,
            radio=radio,
            stations={},
        )
        cb = self._get_current(cmds)
        inter = _fake_interaction()
        await cb(inter)
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.call_args
        assert "Could not reach" in str(args[0])
        assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# /next
# ---------------------------------------------------------------------------


class FakeStation:
    """Minimal stub of bot.main.Station for testing /next."""

    def __init__(self, guild_id: str = "999", listener_count: int = 1) -> None:
        self.guild_id = guild_id
        self.listener_count = listener_count
        self.player = MagicMock()
        self.player.skip = AsyncMock()


class TestNextCommand:
    def _get_next(self, cmds):
        for _, _, cb in cmds:
            if cb.__name__ == "next_command":
                return cb
        raise LookupError("next_command not found")

    async def test_no_active_stations(self, db: Database, state: BotState) -> None:
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        cb = self._get_next(cmds)
        inter = _fake_interaction()
        await cb(inter)
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.call_args
        assert "No active listeners" in str(args[0])

    async def test_skips_on_active_station(self, db: Database, state: BotState) -> None:
        station = FakeStation(guild_id="999", listener_count=2)
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={"999": station},
        )
        cb = self._get_next(cmds)
        inter = _fake_interaction(guild_id=999)
        await cb(inter)
        station.player.skip.assert_awaited_once()
        inter.response.send_message.assert_awaited_once()
        args, _ = inter.response.send_message.call_args
        assert "Skipping" in str(args[0])

    async def test_skips_only_matching_guild(self, db: Database, state: BotState) -> None:
        station_a = FakeStation(guild_id="111", listener_count=1)
        station_b = FakeStation(guild_id="222", listener_count=1)
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={"111": station_a, "222": station_b},
        )
        cb = self._get_next(cmds)
        inter = _fake_interaction(guild_id=111)
        await cb(inter)
        station_a.player.skip.assert_awaited_once()
        station_b.player.skip.assert_not_awaited()

    async def test_ignores_stations_without_listeners(self, db: Database, state: BotState) -> None:
        station = FakeStation(guild_id="999", listener_count=0)
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={"999": station},
        )
        cb = self._get_next(cmds)
        inter = _fake_interaction(guild_id=999)
        await cb(inter)
        station.player.skip.assert_not_awaited()
        args, _ = inter.response.send_message.call_args
        assert "No active listeners" in str(args[0])


# ---------------------------------------------------------------------------
# /leaderboard
# ---------------------------------------------------------------------------

class TestLeaderboardCommand:
    def _get_leaderboard(self, cmds):
        for _, _, cb in cmds:
            if cb.__name__ == "leaderboard_command":
                return cb
        raise LookupError("leaderboard_command not found")

    async def test_empty_leaderboard(self, db: Database, state: BotState) -> None:
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        cb = self._get_leaderboard(cmds)
        inter = _fake_interaction()
        await cb(inter)
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.call_args
        assert "No listening data" in str(args[0])
        assert kwargs.get("ephemeral") is True

    async def test_shows_top_users(self, db: Database, state: BotState) -> None:
        # Seed some listening data.
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, total_seconds_monthly, month_key) "
            "VALUES(?,?,?,?,?)",
            ("u1", "Alice", 36000, 7200, "2025-07"),
        )
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, total_seconds_monthly, month_key) "
            "VALUES(?,?,?,?,?)",
            ("u2", "Bob__with__underscores", 18000, 3600, "2025-07"),
        )

        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        cb = self._get_leaderboard(cmds)
        inter = _fake_interaction()
        await cb(inter)

        inter.response.send_message.assert_awaited_once()
        _, kwargs = inter.response.send_message.call_args
        assert kwargs["ephemeral"] is True
        embed = kwargs["embed"]
        assert embed.title == "📊 Listening Leaderboard"

        # Alice should be first with more hours
        desc = embed.description or ""
        assert "#1" in desc
        assert "Alice" in desc
        assert "Bob" in desc

    async def test_ephemeral_flag_is_set(self, db: Database, state: BotState) -> None:
        cmds = build_commands(
            db=db,
            provider=FakeProvider(),
            state=state,
            radio=FakeRadioClock(),
            stations={},
        )
        cb = self._get_leaderboard(cmds)
        inter = _fake_interaction()
        await cb(inter)
        _, kwargs = inter.response.send_message.call_args
        assert kwargs.get("ephemeral") is True
