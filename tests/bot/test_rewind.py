"""Tests for bot.main.rewind_radio — the /rewind slash-command core.

Uses fake stations/players/provider + a real RadioClock (monkeypatched
monotonic) so the shared-clock maths, the clamp-at-start-of-track edge,
pause handling and the no-listener no-op are all exercised without a live
Discord connection.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.main import RadioClock, rewind_radio
from bot.state import BotState
from provider.client import ProviderError, TrackResponse

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def make_track(**kw) -> TrackResponse:
    base = {
        "track_id": "t1",
        "title": "T1",
        "duration_seconds": 600,
        "local_path": "/cache/t1.mp3",
        "provider_used": "local",
        "playlist_position": 0,
        "ready": True,
    }
    base.update(kw)
    return TrackResponse(**base)


@dataclass
class FakePlayer:
    starts: list[tuple[TrackResponse, float]] = field(default_factory=list)

    async def start(self, track: TrackResponse, *, seek_seconds: float = 0.0) -> None:
        self.starts.append((track, seek_seconds))


@dataclass
class FakeStation:
    guild_id: str = "999"
    listener_count: int = 1
    player: FakePlayer = field(default_factory=FakePlayer)

    def __post_init__(self) -> None:
        self.now_playing = MagicMock()
        self.now_playing.post_or_replace = AsyncMock()


class FakeProvider:
    """Scripted provider: get_by_id from a map, next() from a queue."""

    def __init__(self, tracks: dict[str, TrackResponse] | None = None, next_seq: list | None = None):
        self.tracks = tracks or {}
        self.next_seq = list(next_seq or [])
        self.get_calls: list[str] = []
        self.next_calls = 0
        self.marked_played: list[str] = []

    async def get_by_id(self, track_id: str) -> TrackResponse:
        self.get_calls.append(track_id)
        item = self.tracks.get(track_id)
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise KeyError(track_id)
        return item

    async def next(self) -> TrackResponse:
        self.next_calls += 1
        if not self.next_seq:
            raise ProviderError("playlist empty")
        item = self.next_seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def mark_played(self, track_id: str) -> None:
        self.marked_played.append(track_id)


async def _no_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def monotonic(monkeypatch):
    """Freeze time.monotonic at a fixed value (tests control it directly)."""
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])
    return seq


def _radio(playing: bool, offset: float = 0.0) -> RadioClock:
    radio = RadioClock()
    radio.init_from_state(offset, playing=playing)
    return radio


def _stations(*counts: int) -> dict[str, FakeStation]:
    return {f"g{i}": FakeStation(guild_id=f"g{i}", listener_count=c) for i, c in enumerate(counts)}


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


class TestRewindRejections:
    async def test_rejects_zero_minutes(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track()})
        state.current_track_id = "t1"
        stations = _stations(1)
        result = await rewind_radio(
            minutes=0,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "positive number" in result.message
        assert provider.get_calls == []
        assert radio.position() == 100.0

    async def test_rejects_negative_minutes(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track()})
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=-3,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "positive number" in result.message

    async def test_rejects_non_finite_minutes(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=float("nan"),
            provider=FakeProvider(tracks={"t1": make_track()}),  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "positive number" in result.message

    async def test_no_stations_is_friendly_noop(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track()})
        state.current_track_id = "t1"
        state.playback_position_seconds = 100
        result = await rewind_radio(
            minutes=5,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations={},
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "Nobody is listening" in result.message
        assert provider.get_calls == []
        assert radio.position() == 100.0
        # No mutation at all: persisted position untouched.
        assert state.playback_position_seconds == 100
        assert state.current_track_id == "t1"

    async def test_stations_without_listeners_are_noop(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track()})
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=5,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(0, 0),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "Nobody is listening" in result.message
        assert provider.get_calls == []

    async def test_nothing_playing(self, state: BotState, monotonic, monkeypatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        state.current_track_id = None
        result = await rewind_radio(
            minutes=5,
            provider=FakeProvider(),  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "Nothing is playing" in result.message
        assert radio.position() == 100.0

    async def test_provider_error_is_friendly(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": ProviderError("down")})
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=5,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "Could not reach the file provider" in result.message
        assert radio.position() == 100.0

    async def test_unready_track_is_friendly(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track(ready=False, local_path="")})
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=5,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "isn't ready" in result.message


# ---------------------------------------------------------------------------
# In-track rewind (position math)
# ---------------------------------------------------------------------------


class TestRewindInTrack:
    async def test_seeks_within_current_track(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=300.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await rewind_radio(
            minutes=2,  # -120s -> 180
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 180
        assert result.track_changed is False
        assert radio.is_playing() is True
        assert radio.position() == 180.0
        assert state.playback_position_seconds == 180
        assert state.current_track_id == "t1"
        assert stations["g0"].player.starts == [(track, 180.0)]
        assert provider.next_calls == 0
        assert provider.marked_played == []
        assert "now at 3:00" in result.message
        # Track unchanged -> no Now Playing repost.
        stations["g0"].now_playing.post_or_replace.assert_not_awaited()

    async def test_fractional_minutes(self, state: BotState, monotonic, monkeypatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=300.0)
        provider = FakeProvider(tracks={"t1": make_track(duration_seconds=600)})
        state.current_track_id = "t1"
        result = await rewind_radio(
            minutes=1.5,  # -90s -> 210
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is True
        assert result.new_position_seconds == 210
        assert "Rewound 1.5 minutes" in result.message
        assert "now at 3:30" in result.message

    async def test_skips_stations_without_listeners(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=300.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1, 0)  # second guild has no listeners

        await rewind_radio(
            minutes=2,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert stations["g0"].player.starts == [(track, 180.0)]
        assert stations["g1"].player.starts == []

    async def test_lands_exactly_at_zero(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=120.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"

        result = await rewind_radio(
            minutes=2,  # -120s -> exactly 0
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 0
        assert result.track_changed is False
        assert radio.position() == 0.0
        assert state.current_track_id == "t1"
        assert "now at 0:00" in result.message


# ---------------------------------------------------------------------------
# Start-of-track clamping (never negative, never into the previous track)
# ---------------------------------------------------------------------------


class TestRewindClampAtStart:
    async def test_clamps_at_start_when_rewind_exceeds_position(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """A rewind bigger than the current position parks at 0 — it must NOT
        jump into the previous track (chosen symmetric behaviour to /forward,
        which carries overflow forward)."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=60.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await rewind_radio(
            minutes=5,  # -300s -> target -240 -> clamped to 0
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 0
        assert result.track_changed is False
        assert state.current_track_id == "t1"  # still the same track
        assert radio.position() == 0.0
        assert stations["g0"].player.starts == [(track, 0.0)]
        assert provider.next_calls == 0
        assert provider.marked_played == []
        assert "now at 0:00" in result.message
        # Track unchanged -> no Now Playing repost.
        stations["g0"].now_playing.post_or_replace.assert_not_awaited()

    async def test_huge_rewind_stays_on_current_track(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """Even a massive rewind only clamps at the start of the current track
        — no provider.next(), no mark_played, no track change."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        t1 = make_track(duration_seconds=300)
        t2 = make_track(
            track_id="t2", title="T2", duration_seconds=300, playlist_position=1
        )
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[t2])
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await rewind_radio(
            minutes=60,  # -3600s -> clamped to 0
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 0
        assert result.track_changed is False
        assert state.current_track_id == "t1"
        assert state.playlist_position == 0
        assert provider.next_calls == 0
        assert provider.marked_played == []
        assert stations["g0"].player.starts == [(t1, 0.0)]
        assert "now at 0:00" in result.message


# ---------------------------------------------------------------------------
# Paused interaction
# ---------------------------------------------------------------------------


class TestRewindWhilePaused:
    async def test_rewinds_clock_but_stays_paused(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """Rewind while the radio is paused (admin pause): the clock rewinds and
        stays frozen; stations are NOT restarted; the eventual resume picks up
        the rewound position."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=300.0)  # frozen at 300
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)
        state.is_paused = True

        result = await rewind_radio(
            minutes=2,  # -120s -> 180
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=True,
        )

        assert result.ok is True
        assert result.new_position_seconds == 180
        assert radio.is_playing() is False  # still paused
        assert radio.position() == 180.0  # ... but rewound
        assert state.playback_position_seconds == 180
        assert state.is_paused is True
        assert stations["g0"].player.starts == []  # no player restarted
        assert ", radio is paused." in result.message

    async def test_clamps_at_start_while_paused(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=60.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await rewind_radio(
            minutes=5,  # -300s -> clamped to 0
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=True,
        )

        assert result.ok is True
        assert result.new_position_seconds == 0
        assert radio.is_playing() is False
        assert radio.position() == 0.0
        assert stations["g0"].player.starts == []
        assert ", radio is paused." in result.message


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


class TestRewindLocking:
    async def test_uses_the_shared_advance_lock(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """The whole mutation must happen under the caller-supplied lock so it
        can't race a natural end-of-track advance (same lock the advance loop
        uses)."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=300.0)
        provider = FakeProvider(tracks={"t1": make_track(duration_seconds=600)})
        state.current_track_id = "t1"

        lock = asyncio.Lock()
        await lock.acquire()  # hold the lock: rewind must block, not proceed
        result_future = asyncio.ensure_future(
            rewind_radio(
                minutes=2,
                provider=provider,  # type: ignore[arg-type]
                state=state,
                radio=radio,
                stations=_stations(1),  # type: ignore[arg-type]
                advance_lock=lock,
                admin_paused=False,
            )
        )
        await asyncio.sleep(0.05)
        assert result_future.done() is False  # still waiting on the lock
        lock.release()
        result = await result_future
        assert result.ok is True
