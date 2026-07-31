"""Tests for bot.main.forward_radio — the /forward slash-command core.

Uses fake stations/players/provider + a real RadioClock (monkeypatched
monotonic) so the shared-clock maths, track-boundary clamping, pause handling
and the no-listener no-op are all exercised without a live Discord connection.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.main import RadioClock, forward_radio
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


class TestForwardRejections:
    async def test_rejects_zero_minutes(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track()})
        state.current_track_id = "t1"
        stations = _stations(1)
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
        result = await forward_radio(
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
# In-track skip (position math)
# ---------------------------------------------------------------------------


class TestForwardInTrack:
    async def test_seeks_within_current_track(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await forward_radio(
            minutes=2,  # +120s -> 220 < 600
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 220
        assert result.track_changed is False
        assert radio.is_playing() is True
        assert radio.position() == 220.0
        assert state.playback_position_seconds == 220
        assert state.current_track_id == "t1"
        assert stations["g0"].player.starts == [(track, 220.0)]
        assert provider.next_calls == 0
        assert provider.marked_played == []
        assert "now at 3:40" in result.message
        # Track unchanged -> no Now Playing repost.
        stations["g0"].now_playing.post_or_replace.assert_not_awaited()

    async def test_fractional_minutes(self, state: BotState, monotonic, monkeypatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track(duration_seconds=600)})
        state.current_track_id = "t1"
        result = await forward_radio(
            minutes=1.5,  # +90s -> 190
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is True
        assert result.new_position_seconds == 190
        assert "Skipped forward 1.5 minutes" in result.message
        assert "now at 3:10" in result.message

    async def test_skips_stations_without_listeners(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1, 0)  # second guild has no listeners

        await forward_radio(
            minutes=2,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert stations["g0"].player.starts == [(track, 220.0)]
        assert stations["g1"].player.starts == []


# ---------------------------------------------------------------------------
# Track-boundary clamping (overflow carried into following tracks)
# ---------------------------------------------------------------------------


class TestForwardAcrossTracks:
    async def test_carries_overflow_into_next_track(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=200.0)
        t1 = make_track(duration_seconds=300)
        t2 = make_track(
            track_id="t2", title="T2", duration_seconds=300, playlist_position=1
        )
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[t2])
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await forward_radio(
            minutes=2,  # +120s -> 320: 20s into the next track
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 20
        assert result.track_changed is True
        assert provider.next_calls == 1
        assert provider.marked_played == ["t1"]
        assert state.current_track_id == "t2"
        assert state.playlist_position == 1
        assert radio.position() == 20.0
        assert stations["g0"].player.starts == [(t2, 20.0)]
        # New track -> Now Playing reposted on every station.
        stations["g0"].now_playing.post_or_replace.assert_awaited_once_with(t2)
        assert "on **T2**" in result.message
        assert "now at 0:20" in result.message

    async def test_walks_across_multiple_tracks(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=200.0)
        t1 = make_track(duration_seconds=300)
        t2 = make_track(track_id="t2", title="T2", duration_seconds=300, playlist_position=1)
        t3 = make_track(track_id="t3", title="T3", duration_seconds=300, playlist_position=2)
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[t2, t3])
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await forward_radio(
            minutes=10,  # +600s -> 800: skips t1 fully + t2 fully, lands 200s into t3
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 200
        assert result.track_changed is True
        assert provider.next_calls == 2
        assert provider.marked_played == ["t1", "t2"]
        assert state.current_track_id == "t3"
        assert radio.position() == 200.0
        assert stations["g0"].player.starts == [(t3, 200.0)]
        assert "on **T3**" in result.message

    async def test_lands_at_next_track_start_when_target_exactly_at_end(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=180.0)
        t1 = make_track(duration_seconds=300)
        t2 = make_track(track_id="t2", title="T2", duration_seconds=300, playlist_position=1)
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[t2])
        state.current_track_id = "t1"

        result = await forward_radio(
            minutes=2,  # +120s -> 300 == end of t1 -> next track at 0:00
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 0
        assert result.track_changed is True
        assert state.current_track_id == "t2"

    async def test_clamps_when_next_track_unavailable(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """Provider won't hand over the next track -> park just before the end
        of the current one and let the natural on-finish advance take over."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=200.0)
        t1 = make_track(duration_seconds=300)
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[ProviderError("down")])
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await forward_radio(
            minutes=2,  # +120s -> 320, past t1's 300s end
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert result.ok is True
        assert result.new_position_seconds == 299
        assert result.track_changed is False
        assert state.current_track_id == "t1"
        assert radio.position() == 299.0
        assert stations["g0"].player.starts == [(t1, 299.0)]
        assert "now at 4:59" in result.message

    async def test_caps_the_walk_and_clamps_overflow(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """A huge skip must not walk the playlist forever: after
        FORWARD_MAX_TRACKS the overflow is clamped to the end of the current
        track and the natural on-finish advance continues from there."""
        from bot.main import FORWARD_MAX_TRACKS

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=0.0)
        # 1-second tracks: skipping 10 minutes needs 600 tracks — far past the cap.
        next_seq = [
            make_track(track_id=f"t{i}", title=f"T{i}", duration_seconds=1, playlist_position=i)
            for i in range(1, FORWARD_MAX_TRACKS + 2)
        ]
        provider = FakeProvider(tracks={"t1": make_track(duration_seconds=1)}, next_seq=next_seq)
        state.current_track_id = "t1"

        result = await forward_radio(
            minutes=10,
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )

        assert provider.next_calls == FORWARD_MAX_TRACKS
        assert result.ok is True
        assert result.new_position_seconds == 0  # clamped to end-1 of a 1s track
        assert state.current_track_id == f"t{FORWARD_MAX_TRACKS}"


# ---------------------------------------------------------------------------
# Paused interaction
# ---------------------------------------------------------------------------


class TestForwardWhilePaused:
    async def test_advances_clock_but_stays_paused(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """Skip while the radio is paused (admin pause): the clock advances and
        stays frozen; stations are NOT restarted; the eventual resume picks up
        the advanced position."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=100.0)  # frozen at 100
        track = make_track(duration_seconds=600)
        provider = FakeProvider(tracks={"t1": track})
        state.current_track_id = "t1"
        stations = _stations(1)
        state.is_paused = True

        result = await forward_radio(
            minutes=2,  # +120s -> 220
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=True,
        )

        assert result.ok is True
        assert result.new_position_seconds == 220
        assert radio.is_playing() is False  # still paused
        assert radio.position() == 220.0  # ... but advanced
        assert state.playback_position_seconds == 220
        assert state.is_paused is True
        assert stations["g0"].player.starts == []  # no player restarted
        assert ", radio is paused." in result.message

    async def test_crosses_tracks_while_paused(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=False, offset=200.0)
        t1 = make_track(duration_seconds=300)
        t2 = make_track(track_id="t2", title="T2", duration_seconds=300, playlist_position=1)
        provider = FakeProvider(tracks={"t1": t1}, next_seq=[t2])
        state.current_track_id = "t1"
        stations = _stations(1)

        result = await forward_radio(
            minutes=2,  # +120s -> 320 -> 20s into t2
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=True,
        )

        assert result.ok is True
        assert result.new_position_seconds == 20
        assert result.track_changed is True
        assert state.current_track_id == "t2"
        assert radio.is_playing() is False
        assert radio.position() == 20.0
        assert stations["g0"].player.starts == []
        # Track changed while paused -> Now Playing still reposted.
        stations["g0"].now_playing.post_or_replace.assert_awaited_once_with(t2)


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


class TestForwardLocking:
    async def test_uses_the_shared_advance_lock(
        self, state: BotState, monotonic, monkeypatch
    ) -> None:
        """The whole mutation must happen under the caller-supplied lock so it
        can't race a natural end-of-track advance (same lock the advance loop
        uses)."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        radio = _radio(playing=True, offset=100.0)
        provider = FakeProvider(tracks={"t1": make_track(duration_seconds=600)})
        state.current_track_id = "t1"

        lock = asyncio.Lock()
        await lock.acquire()  # hold the lock: forward must block, not proceed
        result_future = asyncio.ensure_future(
            forward_radio(
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
