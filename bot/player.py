"""FFmpeg-based audio player.

Design:

* `Player` owns the elapsed clock. When a track starts, we record `started_at`
  (monotonic) and the resume offset (`resume_from`). Elapsed at any moment is
  `resume_from + (monotonic() - started_at)`.
* When we pause (channel empty), we compute elapsed, persist it via `BotState`,
  and stop the audio source (bot stays connected).
* When we resume, we re-fetch the file via the provider and start a new FFmpeg
  process with `-ss <resume_from>`.
* Track finishing (natural end) triggers the `on_finish` callback the caller
  registered. The callback is what advances the playlist and reposts the
  "Now Playing" embed.

`discord.FFmpegPCMAudio` and the voice client are injected via the constructor,
so unit tests can substitute fakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bot.state import BotState
from provider.client import FileProviderClient, TrackResponse

log = logging.getLogger(__name__)

FinishCallback = Callable[["Player", TrackResponse], Awaitable[None]]


@dataclass(slots=True)
class ElapsedClock:
    """Monotonic elapsed-seconds clock. Extracted so it can be unit-tested."""

    resume_from: float = 0.0
    started_at: float | None = None  # monotonic timestamp

    def start(self, resume_from: float = 0.0, *, now: float | None = None) -> None:
        self.resume_from = max(0.0, resume_from)
        self.started_at = now if now is not None else time.monotonic()

    def stop(self, *, now: float | None = None) -> float:
        """Freeze the clock. Returns final elapsed seconds."""
        elapsed = self.elapsed(now=now)
        self.resume_from = elapsed
        self.started_at = None
        return elapsed

    def elapsed(self, *, now: float | None = None) -> float:
        if self.started_at is None:
            return self.resume_from
        cur = now if now is not None else time.monotonic()
        return self.resume_from + max(0.0, cur - self.started_at)

    def reset(self) -> None:
        self.resume_from = 0.0
        self.started_at = None


# Injected factories so tests can substitute a fake audio source without
# needing FFmpeg on the machine running the tests.
FFmpegSourceFactory = Callable[[str, float], object]  # (path, seek_seconds) -> AudioSource


def default_ffmpeg_source(path: str, seek_seconds: float):  # pragma: no cover — requires ffmpeg
    """Build a discord.FFmpegPCMAudio for `path`, seeking to `seek_seconds`."""
    import discord  # local import — discord.py is heavy and optional in tests

    before = ""
    if seek_seconds > 0:
        # -ss before -i is fast (keyframe) seek; good enough for audio.
        before = f"-ss {seek_seconds:.3f}"
    # -vn drops any video stream just in case the source has one.
    # -loglevel warning keeps the ffmpeg subprocess quiet.
    return discord.FFmpegPCMAudio(
        path,
        before_options=before,
        options="-vn -loglevel warning",
    )


class Player:
    """High-level audio playback controller.

    Args:
        voice_client: A `discord.VoiceClient`-like object. The player calls
            `.play(source, after=cb)`, `.stop()`, and `.is_playing()`. Injected
            so tests can pass a fake.
        provider: HTTP client for fetching the current/next track.
        state: BotState — the player persists position + track id on transitions.
        loop: The asyncio loop the discord.py client runs on. Used to schedule
            the on-finish callback from the FFmpeg thread.
        source_factory: Builds an audio source from (path, seek_seconds).
    """

    def __init__(
        self,
        *,
        voice_client,
        provider: FileProviderClient,
        state: BotState,
        loop: asyncio.AbstractEventLoop,
        source_factory: FFmpegSourceFactory = default_ffmpeg_source,
    ) -> None:
        self.voice_client = voice_client
        self.provider = provider
        self.state = state
        self.loop = loop
        self.source_factory = source_factory
        self.clock = ElapsedClock()
        self.current_track: TrackResponse | None = None
        self._on_finish: FinishCallback | None = None
        # Set while we're intentionally stopping (pause / stop_hard) so the
        # after-callback doesn't misinterpret it as a natural finish.
        self._suppress_finish = False
        # Monotonically increasing playback session id. Each `_start_locked`
        # bumps it and stamps the closure with the new value; when the FFmpeg
        # `after` callback for an old session fires, we ignore it. This
        # prevents "double advance" when start() interrupts an existing track.
        self._play_seq = 0
        self._lock = asyncio.Lock()

    # -------------------------------------------------------- registration
    def on_finish(self, cb: FinishCallback) -> None:
        self._on_finish = cb

    # ----------------------------------------------------------- accessors
    def is_playing(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_playing())

    def elapsed_seconds(self) -> int:
        return int(self.clock.elapsed())

    # ----------------------------------------------------------- lifecycle
    async def start(self, track: TrackResponse, *, seek_seconds: float = 0.0) -> None:
        """Begin playback of `track` from `seek_seconds`."""
        async with self._lock:
            await self._start_locked(track, seek_seconds=seek_seconds)

    async def _start_locked(self, track: TrackResponse, *, seek_seconds: float) -> None:
        # Stop any previous playback. We DON'T need to set _suppress_finish
        # here — the seq check below discards the old callback safely.
        if self.voice_client.is_playing():
            self.voice_client.stop()
        self._suppress_finish = False

        self._play_seq += 1
        my_seq = self._play_seq

        source = self.source_factory(track.local_path, seek_seconds)
        self.current_track = track
        self.clock.start(resume_from=seek_seconds)

        self.state.current_track_id = track.track_id
        self.state.playback_position_seconds = int(seek_seconds)
        self.state.playlist_position = track.playlist_position
        self.state.is_paused = False

        # `after` runs on FFmpeg's cleanup thread — schedule the callback back
        # onto the main asyncio loop. The captured `my_seq` lets us discard
        # after-callbacks from previously-superseded sources.
        def _after_bound(exc: BaseException | None, _seq: int = my_seq) -> None:
            self._after(exc, expected_seq=_seq)

        self.voice_client.play(source, after=_after_bound)

    def _after(self, exc: BaseException | None, *, expected_seq: int | None = None) -> None:
        if exc is not None:
            log.warning("ffmpeg after-callback error: %s", exc)
        if expected_seq is not None and expected_seq != self._play_seq:
            # This callback belongs to a superseded playback session — either
            # we started a new track already, or we paused/stopped. Discard.
            log.debug(
                "ignoring after-callback for stale seq %s (current %s)",
                expected_seq,
                self._play_seq,
            )
            return
        if self._suppress_finish:
            return
        cb = self._on_finish
        track = self.current_track
        if cb is None or track is None:
            return
        # Bail out cleanly if the loop is closed (shutdown mid-track). Building
        # the coroutine only after the check means we don't leak an
        # unawaited coroutine.
        if self.loop.is_closed():
            return
        coro = cb(self, track)
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as err:  # pragma: no cover — loop closed between check & schedule
            log.warning("could not schedule on_finish: %s", err)
            coro.close()

    async def pause(self) -> None:
        """Stop audio, keep the position for later resume."""
        async with self._lock:
            # Bump the seq so the after-callback from ffmpeg's stop is
            # discarded — belt-and-braces on top of _suppress_finish.
            self._play_seq += 1
            self._suppress_finish = True
            if self.voice_client.is_playing():
                self.voice_client.stop()
            elapsed = self.clock.stop()
            self.state.playback_position_seconds = int(elapsed)
            self.state.is_paused = True

    async def resume(self) -> None:
        """Resume playback of the current track at the saved position.

        Re-fetches the track via the provider in case the cache evicted it.
        """
        async with self._lock:
            track_id = self.state.current_track_id
            if not track_id:
                # Nothing to resume — caller should call `start_current()` instead.
                return
            resume_at = float(self.state.playback_position_seconds)
            # Even if the cache still has it, /tracks/{id} guarantees it.
            track = await self.provider.get_by_id(track_id)
            await self._start_locked(track, seek_seconds=resume_at)

    async def skip(self) -> None:
        """Force-finish the current track and advance."""
        async with self._lock:
            if self.voice_client.is_playing():
                # Do NOT suppress finish: we want the on_finish callback to
                # fire so the playlist advances naturally.
                self.voice_client.stop()

    async def stop_hard(self) -> None:
        """Stop audio, do not advance, do not persist position (used on shutdown)."""
        async with self._lock:
            self._play_seq += 1
            self._suppress_finish = True
            if self.voice_client.is_playing():
                self.voice_client.stop()
