"""Player tests — use a FakeVoiceClient so no FFmpeg/Discord is needed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from bot.player import ElapsedClock, Player
from bot.state import BotState
from provider.client import TrackResponse


# ---------------------------------------------------------------- fake infra
@dataclass
class FakeVoiceClient:
    playing: bool = False
    last_source: object | None = None
    after_cb: object | None = None
    stop_calls: int = 0
    play_calls: int = 0

    def play(self, source, after=None):
        self.playing = True
        self.last_source = source
        self.after_cb = after
        self.play_calls += 1

    def stop(self):
        self.playing = False
        self.stop_calls += 1

    def is_playing(self):
        return self.playing


class FakeProvider:
    """Minimal shim mirroring provider.client.FileProviderClient's methods used by Player."""

    def __init__(self, tracks: dict[str, TrackResponse]) -> None:
        self.tracks = tracks
        self.get_by_id_calls: list[str] = []

    async def get_by_id(self, track_id: str) -> TrackResponse:
        self.get_by_id_calls.append(track_id)
        return self.tracks[track_id]


def make_track(**overrides: Any) -> TrackResponse:
    base = {
        "track_id": "t1",
        "title": "T1",
        "duration_seconds": 300,
        "local_path": "/cache/t1.mp3",
        "provider_used": "local",
        "playlist_position": 0,
        "ready": True,
    }
    base.update(overrides)
    return TrackResponse(**base)


@dataclass
class RecordingSource:
    path: str
    seek: float
    label: str = "src"


@dataclass
class Ctx:
    voice: FakeVoiceClient
    provider: FakeProvider
    player: Player
    state: BotState
    sources: list[RecordingSource] = field(default_factory=list)


async def _build_ctx(state: BotState) -> Ctx:
    """Async factory so we can grab the *running* loop, not a stale global one."""
    voice = FakeVoiceClient()
    tracks = {"t1": make_track(), "t2": make_track(track_id="t2", title="T2", playlist_position=1)}
    provider = FakeProvider(tracks)
    sources: list[RecordingSource] = []

    def factory(path: str, seek: float) -> RecordingSource:
        s = RecordingSource(path=path, seek=seek)
        sources.append(s)
        return s

    player = Player(
        voice_client=voice,
        provider=provider,  # type: ignore[arg-type]
        state=state,
        loop=asyncio.get_running_loop(),
        source_factory=factory,
    )
    return Ctx(voice=voice, provider=provider, player=player, state=state, sources=sources)


@pytest.fixture
async def ctx(state: BotState) -> Ctx:
    return await _build_ctx(state)


# --------------------------------------------------------------- ElapsedClock
class TestElapsedClock:
    def test_before_start_returns_resume_from(self) -> None:
        c = ElapsedClock(resume_from=10)
        assert c.elapsed(now=1_000_000) == 10

    def test_start_at_zero(self) -> None:
        c = ElapsedClock()
        c.start(now=100.0)
        assert c.elapsed(now=105.0) == 5.0

    def test_start_with_resume(self) -> None:
        c = ElapsedClock()
        c.start(resume_from=60, now=100.0)
        assert c.elapsed(now=110.0) == 70.0

    def test_stop_freezes(self) -> None:
        c = ElapsedClock()
        c.start(resume_from=30, now=100.0)
        assert c.stop(now=110.0) == 40.0
        assert c.elapsed(now=200.0) == 40.0  # no further advance after stop

    def test_reset(self) -> None:
        c = ElapsedClock(resume_from=42)
        c.start(now=0)
        c.reset()
        assert c.elapsed(now=100) == 0.0

    def test_never_negative_after_clock_jump_back(self) -> None:
        c = ElapsedClock()
        c.start(resume_from=10, now=100.0)
        # If monotonic somehow ticks backward we should not go below resume_from.
        assert c.elapsed(now=99.0) == 10.0


# ---------------------------------------------------------------------- Player
class TestPlayerStart:
    async def test_start_plays_source(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track())
        assert ctx.voice.play_calls == 1
        assert ctx.voice.is_playing()
        assert ctx.sources[0].path == "/cache/t1.mp3"
        assert ctx.sources[0].seek == 0.0

    async def test_start_persists_state(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track(track_id="t1", playlist_position=5))
        assert ctx.state.current_track_id == "t1"
        assert ctx.state.playlist_position == 5
        assert ctx.state.is_paused is False

    async def test_start_with_seek(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track(), seek_seconds=42.0)
        assert ctx.sources[0].seek == 42.0
        assert ctx.state.playback_position_seconds == 42

    async def test_start_stops_previous(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track())
        await ctx.player.start(make_track(track_id="t2", playlist_position=1))
        # stop() was called for the previous playback
        assert ctx.voice.stop_calls >= 1
        assert ctx.voice.play_calls == 2


class TestPlayerPause:
    async def test_pause_stops_and_persists(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track())
        await asyncio.sleep(0.02)
        await ctx.player.pause()
        assert ctx.voice.is_playing() is False
        assert ctx.state.is_paused is True
        assert ctx.state.playback_position_seconds >= 0

    async def test_pause_does_not_trigger_on_finish(self, ctx: Ctx) -> None:
        called: list[TrackResponse] = []

        async def cb(_p, t):
            called.append(t)

        ctx.player.on_finish(cb)
        await ctx.player.start(make_track())
        await ctx.player.pause()
        # Simulate ffmpeg after-callback firing (as if stop caused it)
        if ctx.voice.after_cb:
            ctx.voice.after_cb(None)
        await asyncio.sleep(0.05)
        assert called == []


class TestPlayerResume:
    async def test_resume_refetches_and_plays(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track())
        await ctx.player.pause()
        ctx.state.playback_position_seconds = 90
        await ctx.player.resume()
        assert "t1" in ctx.provider.get_by_id_calls
        assert ctx.sources[-1].seek == 90.0
        assert ctx.state.is_paused is False

    async def test_resume_with_no_current_track_noop(self, ctx: Ctx) -> None:
        # Nothing started yet, no state — resume should be a no-op.
        await ctx.player.resume()
        assert ctx.voice.play_calls == 0


class TestPlayerSkipAndStop:
    async def test_skip_calls_stop_and_lets_on_finish_fire(self, ctx: Ctx) -> None:
        called: list[TrackResponse] = []

        async def cb(_p, t):
            called.append(t)

        ctx.player.on_finish(cb)
        await ctx.player.start(make_track())
        await ctx.player.skip()
        # Manually fire the after-callback to simulate FFmpeg finishing.
        if ctx.voice.after_cb:
            ctx.voice.after_cb(None)
        # Give the event loop a tick for run_coroutine_threadsafe.
        await asyncio.sleep(0.05)
        assert len(called) == 1
        assert called[0].track_id == "t1"

    async def test_stop_hard_suppresses_finish(self, ctx: Ctx) -> None:
        called: list[TrackResponse] = []

        async def cb(_p, t):
            called.append(t)

        ctx.player.on_finish(cb)
        await ctx.player.start(make_track())
        await ctx.player.stop_hard()
        if ctx.voice.after_cb:
            ctx.voice.after_cb(None)
        await asyncio.sleep(0.05)
        assert called == []


class TestElapsedReporting:
    async def test_elapsed_advances(self, ctx: Ctx) -> None:
        await ctx.player.start(make_track(), seek_seconds=5)
        await asyncio.sleep(0.05)
        assert ctx.player.elapsed_seconds() >= 5
