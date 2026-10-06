"""Voice flap detector + full-reset policy for Discord-side kicks.

Incident 2026-10-06 (#27, recurrence of the 2026-10-04 storm): after a
transient voice-WS 1006, Discord kept kicking the bot with 4014/4022 ~30s
after every successful connect. discord.py's internal ``_potential_reconnect``
then fails (``Reconnect was unsuccessful``) and the watchdog in ``bot.main``
rejoins within seconds — looping forever (~120 cycles/hour) until a manual
container restart cleared the server-side ghost session.

This module counts ``bot was disconnected by Discord`` events per guild. Once
``FLAP_THRESHOLD`` kicks land inside ``FLAP_WINDOW_SECONDS`` the station is
*flapping*: the next reconnect does a full reset — ``disconnect(force=True)``,
exponential backoff, then a fresh ``channel.connect(reconnect=False)`` that
does not rely on discord.py's resume path. A plain container restart proved
this clears the ghost, so the bot now does the equivalent automatically.

``voice_cycles_total`` is the per-guild disconnect counter (flap metric). The
Gatus silence monitor stays green during a storm because recovery *works* —
``bot.main`` marks flapping stations so ``is_radio_healthy`` reports
unhealthy while a flap is active and the external alert fires.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

FLAP_WINDOW_SECONDS = 300.0  # kicks inside this window count toward a flap
FLAP_THRESHOLD = 3  # kicks in-window that declare a flap
BASE_BACKOFF_SECONDS = 2.0  # first full-reset backoff; doubles per extra kick
MAX_BACKOFF_SECONDS = 60.0  # backoff cap so recovery stays prompt

# Close codes discord.py surfaces when the *server* kicks the bot off voice
# (externally disconnected → `_potential_reconnect` → "Reconnect was
# unsuccessful"). Logged on every drop so the kick signature is greppable.
EXTERNAL_KICK_CODES = frozenset({4014, 4022})


@dataclass(slots=True)
class FlapDecision:
    """Outcome of recording one voice disconnect for a guild."""

    is_flap: bool  # kick count hit FLAP_THRESHOLD inside the window
    kick_count: int  # kicks in-window including this one
    backoff_seconds: float  # full-reset backoff for this kick count


def backoff_for(kick_count: int) -> float:
    """Exponential backoff for the Nth in-window kick (capped)."""
    extra = max(0, kick_count - FLAP_THRESHOLD)
    return min(BASE_BACKOFF_SECONDS * (2.0**extra), MAX_BACKOFF_SECONDS)


def close_code_str(close_code: int | None) -> str:
    """Render a voice close code for logs (never blank)."""
    return str(close_code) if close_code is not None else "unknown"


def extract_close_code(voice_client: object | None) -> int | None:
    """Best-effort voice WS close code from a discord.py VoiceClient.

    discord.py does not surface the code on the voice_state_update path, so
    probe the usual internals; returns None when nothing exposes it (logged
    as "unknown" rather than dropped).
    """
    if voice_client is None:
        return None
    for attr in ("ws", "_ws", "socket"):
        with contextlib.suppress(Exception):
            ws = getattr(voice_client, attr)
            for code_attr in ("close_code", "_close_code", "code"):
                with contextlib.suppress(Exception):
                    code = getattr(ws, code_attr)
                    if isinstance(code, bool):
                        continue
                    if isinstance(code, (int, float)):
                        return int(code)
    return None


class VoiceFlapTracker:
    """Per-guild sliding-window kick counter + backoff policy."""

    def __init__(
        self,
        *,
        window_seconds: float = FLAP_WINDOW_SECONDS,
        threshold: int = FLAP_THRESHOLD,
    ) -> None:
        self.window_seconds = window_seconds
        self.threshold = threshold
        self._kicks: dict[str, collections.deque[float]] = {}
        self._cycles: dict[str, int] = {}  # voice_cycles_total per guild

    def record_disconnect(
        self,
        guild_id: str,
        *,
        close_code: int | None = None,
        now: float | None = None,
    ) -> FlapDecision:
        """Record one Discord-side disconnect; total++ and flap verdict."""
        cur = now if now is not None else time.monotonic()
        self._cycles[guild_id] = self._cycles.get(guild_id, 0) + 1
        buf = self._kicks.setdefault(guild_id, collections.deque())
        buf.append(cur)
        cutoff = cur - self.window_seconds
        while buf and buf[0] < cutoff:
            buf.popleft()
        count = len(buf)
        return FlapDecision(
            is_flap=count >= self.threshold,
            kick_count=count,
            backoff_seconds=backoff_for(count),
        )

    def kick_count(self, guild_id: str, *, now: float | None = None) -> int:
        """In-window kick count for a guild (expiry applied)."""
        cur = now if now is not None else time.monotonic()
        buf = self._kicks.get(guild_id, collections.deque())
        cutoff = cur - self.window_seconds
        while buf and buf[0] < cutoff:
            buf.popleft()
        return len(buf)

    def is_flapping(self, guild_id: str, *, now: float | None = None) -> bool:
        """True once in-window kicks hit the flap threshold."""
        return self.kick_count(guild_id, now=now) >= self.threshold

    def backoff_for_guild(self, guild_id: str, *, now: float | None = None) -> float:
        """Current full-reset backoff for a guild (0 when not flapping)."""
        count = self.kick_count(guild_id, now=now)
        if count < self.threshold:
            return 0.0
        return backoff_for(count)

    def voice_cycles_total(self, guild_id: str) -> int:
        """Total Discord-side disconnects recorded for a guild (metric)."""
        return self._cycles.get(guild_id, 0)


_default_tracker = VoiceFlapTracker()


def default_tracker() -> VoiceFlapTracker:
    """Process-wide tracker used by bot.main (tests inject their own)."""
    return _default_tracker


def record_voice_cycle(
    guild_id: str, *, close_code: int | None = None, now: float | None = None
) -> FlapDecision:
    """Record one Discord-side disconnect on the default tracker."""
    return _default_tracker.record_disconnect(guild_id, close_code=close_code, now=now)


def voice_cycles_total(guild_id: str) -> int:
    """Total Discord-side disconnects recorded for a guild (metric)."""
    return _default_tracker.voice_cycles_total(guild_id)
