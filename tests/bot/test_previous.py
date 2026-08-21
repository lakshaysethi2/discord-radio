"""Tests for bot.main.previous_radio — the /previous slash-command core."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from bot.main import RadioClock, previous_radio
from bot.state import BotState
from provider.client import ProviderError, TrackResponse


def make_track(**kw) -> TrackResponse:
    base = {
        "track_id": "t0",
        "title": "T0",
        "duration_seconds": 600,
        "local_path": "/cache/t0.mp3",
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
    def __init__(self, seq: list | None = None) -> None:
        self.seq = list(seq or [])
        self.calls = 0

    async def previous(self) -> TrackResponse:
        self.calls += 1
        if not self.seq:
            raise ProviderError("playlist empty")
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _radio(playing: bool, offset: float = 0.0) -> RadioClock:
    radio = RadioClock()
    radio.init_from_state(offset, playing=playing)
    return radio


def _stations(*counts: int) -> dict[str, FakeStation]:
    return {f"g{i}": FakeStation(guild_id=f"g{i}", listener_count=c) for i, c in enumerate(counts)}


class TestPreviousRadio:
    async def test_no_listeners_is_noop(self, state: BotState) -> None:
        provider = FakeProvider(seq=[make_track()])
        radio = _radio(playing=False)
        result = await previous_radio(
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=_stations(0),
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "No active listeners" in result.message
        assert provider.calls == 0

    async def test_steps_back_and_restarts_listening_stations(self, state: BotState) -> None:
        prev = make_track()
        provider = FakeProvider(seq=[prev])
        radio = _radio(playing=True, offset=90.0)
        state.current_track_id = "t1"
        stations = _stations(1, 0)
        result = await previous_radio(
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is True
        assert result.track_id == "t0"
        assert "T0" in result.message
        assert radio.position() < 0.1
        assert state.playback_position_seconds == 0
        assert stations["g0"].player.starts == [(prev, 0.0)]
        assert stations["g1"].player.starts == []
        stations["g0"].now_playing.post_or_replace.assert_awaited_once_with(prev)
        stations["g1"].now_playing.post_or_replace.assert_awaited_once_with(prev)

    async def test_paused_radio_parks_at_zero_without_restarting(self, state: BotState) -> None:
        prev = make_track()
        provider = FakeProvider(seq=[prev])
        radio = _radio(playing=False, offset=40.0)
        stations = _stations(1)
        result = await previous_radio(
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=radio,
            stations=stations,  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=True,
        )
        assert result.ok is True
        assert radio.is_playing() is False
        assert radio.position() == 0
        assert state.current_track_id == "t0"
        assert stations["g0"].player.starts == []

    async def test_provider_error(self, state: BotState) -> None:
        provider = FakeProvider(seq=[ProviderError("down")])
        result = await previous_radio(
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=_radio(playing=True),
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "Could not reach" in result.message

    async def test_not_ready_track(self, state: BotState) -> None:
        provider = FakeProvider(seq=[make_track(ready=False, local_path="")])
        result = await previous_radio(
            provider=provider,  # type: ignore[arg-type]
            state=state,
            radio=_radio(playing=True),
            stations=_stations(1),  # type: ignore[arg-type]
            advance_lock=asyncio.Lock(),
            admin_paused=False,
        )
        assert result.ok is False
        assert "isn't ready" in result.message
