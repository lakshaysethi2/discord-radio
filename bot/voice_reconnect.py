"""Per-guild voice reconnect guard: one owner per guild, no self-fighting.

Incident 2026-10-07 (recurrence of #27, which PR #28 did not actually fix):
the bot joined voice, then left and rejoined every ~30s for hours. The cause
was not a Discord ghost session — it was our own code racing discord.py.

Two paths reconnected voice for the same guild:

* ``on_voice_state_update`` calls ``ensure_station_voice_connected``.
* the 5s watchdog loop in ``bot.main`` calls it too.

Both did ``disconnect(force=True)`` and then ``channel.connect(...)``. At the
same time discord.py's own ``VoiceConnectionState._potential_reconnect`` was
already reconnecting (it waits up to its 30s timeout for a voice server
update). Our ``disconnect(force=True)`` discarded that in-flight reconnect,
and the abandoned flow then closed the socket the watchdog had just opened
("Disconnecting from voice normally, close code 1000") — every ~30s, forever.

This module makes exactly one reconnect run per guild at a time: concurrent
callers await the same in-flight task instead of starting a competing
handshake. It also gives discord.py's own reconnect a short grace period to
finish before we take over, which is what stops the fight in the common case.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

log = logging.getLogger(__name__)

# How long to let discord.py's own reconnect finish before we take over.
# `_potential_reconnect` waits up to its own 30s timeout, so a long grace here
# would just add delay; 8s covers the normal handshake (sub-second in logs)
# while still handing off promptly when discord.py is truly stuck.
DISCORD_RECONNECT_GRACE_SECONDS = 8.0
# Poll step while waiting for that grace period.
_GRACE_POLL_SECONDS = 0.5


def is_voice_connected(voice_client: object | None) -> bool:
    """True when a voice client exists and reports itself connected."""
    return voice_client is not None and bool(
        getattr(voice_client, "is_connected", lambda: False)()
    )


def is_discord_reconnecting(voice_client: object | None) -> bool:
    """True when discord.py is mid-reconnect on this voice client.

    discord.py keeps the ``VoiceClient`` object across a reconnect and exposes
    the underlying ``VoiceConnectionState`` as ``_connection`` (2.x). While its
    ``_runner`` task is alive the library owns the connection, so we must not
    force-disconnect it — doing so is what caused the 2026-10-07 storm.
    """
    if voice_client is None:
        return False
    state = getattr(voice_client, "_connection", None)
    if state is None:
        return False
    runner = getattr(state, "runner", None)
    if runner is None:
        return False
    with contextlib.suppress(Exception):
        return not runner.done()
    return False


class GuildReconnectGuard:
    """Serializes voice reconnects per guild; concurrent callers share a task.

    The first caller for a guild starts the reconnect coroutine; everyone else
    awaits the same task. That is what prevents the double-handshake race
    between the event handler and the watchdog.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[bool]] = {}

    def inflight(self, guild_id: str) -> bool:
        """True while a reconnect for this guild is already running."""
        task = self._inflight.get(guild_id)
        return task is not None and not task.done()

    async def run(self, guild_id: str, factory) -> bool:  # type: ignore[no-untyped-def]
        """Run ``factory()`` once per guild; concurrent callers share it.

        The task is cached before the first await, so a second caller arriving
        while the first reconnect is in flight awaits the same task instead of
        starting a competing handshake. Each awaiting caller gets its own
        ``shield`` wrapper so one caller's cancellation does not abort the
        reconnect or the other waiters.
        """
        task = self._inflight.get(guild_id)
        if task is None or task.done():
            task = asyncio.ensure_future(factory())
            self._inflight[guild_id] = task
            task.add_done_callback(lambda t, gid=guild_id: self._forget(gid, t))
        return await asyncio.shield(task)

    def _forget(self, guild_id: str, task: asyncio.Task[bool]) -> None:
        """Drop a finished task so the next reconnect starts fresh."""
        if self._inflight.get(guild_id) is task:
            self._inflight.pop(guild_id, None)
        # Surface exceptions instead of letting asyncio log "never retrieved".
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()


async def wait_for_discord_reconnect(
    station_voice_client_getter,  # type: ignore[no-untyped-def]
    *,
    grace_seconds: float = DISCORD_RECONNECT_GRACE_SECONDS,
    sleep=None,  # type: ignore[no-untyped-def]
) -> bool:
    """Give discord.py's in-flight reconnect a chance to finish on its own.

    Returns True if the call site observed a live connection by the end of the
    grace period, in which case the caller must NOT start its own handshake.
    """
    _sleep = sleep or asyncio.sleep
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        vc = station_voice_client_getter()
        if is_voice_connected(vc):
            return True
        if not is_discord_reconnecting(vc):
            return is_voice_connected(vc)
        await _sleep(_GRACE_POLL_SECONDS)
    vc = station_voice_client_getter()
    still_reconnecting = is_discord_reconnecting(vc)
    if still_reconnecting:
        log.warning("voice reconnect: discord.py still reconnecting after grace period")
    return is_voice_connected(vc)


def default_guard() -> GuildReconnectGuard:
    """Process-wide guard used by bot.main (tests inject their own)."""
    return _default_guard


_default_guard = GuildReconnectGuard()
