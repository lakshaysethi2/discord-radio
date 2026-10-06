"""Gatus voice-connection heartbeat for the Discord radio.

The radio's silent failure mode is a dropped Discord *voice* connection while
the process keeps running: the bot stays gateway-connected and logs nothing,
but every listener join then raises "Not connected to voice". This module
pushes the live voice-connection state to a Gatus external endpoint

    POST {GATUS_PUSH_URL}/api/v1/endpoints/radio_discord-radio-voice/external?success={true|false}

so Gatus (and its telegram alert) notices the drop immediately instead of
waiting for its 60s heartbeat timeout.

Push cadence is ``interval_seconds`` (default 30) — comfortably inside Gatus's
60s window. On a healthy→unhealthy transition we push ``success=false`` once
with a short error text so the alert fires immediately; on recovery we push
``success=true`` right away and resume the regular cadence. Push failures are
logged and swallowed — a failed push simply means no heartbeat arrives, which
is exactly when the alert should fire.

The feature is fully disabled unless ``GATUS_PUSH_URL`` and
``GATUS_PUSH_TOKEN`` are set (see ``bot.config``); wiring in ``bot.main`` only
starts the task when both are present.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from bot.voice_flap import VoiceFlapTracker
from bot.voice_flap import default_tracker as _default_flap_tracker

log = logging.getLogger(__name__)

# Captain 2026-08-08: radio must never be silent >10s when someone is in
# voice (5s ideal, 10s hard limit). 60s was way too much.
SILENCE_THRESHOLD_SECONDS = 10.0

# Gatus derives the external-endpoint key from group "radio" + name
# "discord-radio voice" (spaces -> dashes): radio_discord-radio-voice.
# Keep this key exactly as the companion gatus-side config expects it.
ENDPOINT_PATH = "/api/v1/endpoints/radio_discord-radio-voice/external"

# Short error text shown in the Gatus alert when the voice link is down.
UNHEALTHY_ERROR = "voice disconnected"


class HeartbeatHttpClient(Protocol):
    """Duck-typed surface: anything with an async ``post`` (httpx.AsyncClient…).

    ``httpx.AsyncClient`` satisfies this structurally; tests inject fakes.
    """

    async def post(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> Any: ...


def is_radio_healthy(
    stations: Mapping[str, object],
    *,
    now: float | None = None,
    silence_threshold: float = SILENCE_THRESHOLD_SECONDS,
    flap_tracker: VoiceFlapTracker | None = None,
) -> bool:
    """True when the radio is healthy for the current listening state.

    Rules (captain, 2026-08-08):

    * Nobody listening anywhere → healthy even if silent — silence is fine
      when the pausing logic (``RadioClock`` + ``should_pause``) froze it.
      Radio is PAUSED when 0 listeners.
    * Someone is listening (>=1 in voice) → unhealthy if voice is
      disconnected OR playback has been silent for more than
      ``silence_threshold`` (default 10s, 5s ideal). That catches a frozen
      FUSE (dead ``tvbot-rclone-mount``), stuck file-provider, or dropped
      voice/ffmpeg empty output.
    * A station flapping (≥3 Discord kicks in 5 min, issue #27) is unhealthy
      even while silence recovery keeps restarting audio — the recovery
      *works*, so without this the leave/rejoin storm stays invisible to the
      monitor. Window-based: clears itself once kicks age out.

    ``stations`` is the authoritative per-guild structure built in
    ``bot.main``: Station objects only for *enabled* guilds that joined
    voice. A dropped voice connection keeps the Station —
    ``voice_client.is_connected()`` reveals it.
    """
    # No listeners anywhere: silence is intentional (radio is paused).
    any_listeners = any(getattr(st, "listener_count", 0) > 0 for st in stations.values())
    if not any_listeners:
        return True
    # Flap check first: a leave/rejoin storm self-heals audio each cycle, so
    # the per-station checks below would all pass while Discord keeps kicking.
    tracker = flap_tracker if flap_tracker is not None else _default_flap_tracker()
    cur = now if now is not None else time.monotonic()
    for st in stations.values():
        if getattr(st, "listener_count", 0) <= 0:
            continue
        gid = getattr(st, "guild_id", None)
        if gid is not None and tracker.is_flapping(str(gid), now=cur):
            return False
    # Someone is listening — every listening station must be healthy.
    for st in stations.values():
        if getattr(st, "listener_count", 0) <= 0:
            continue
        vc = getattr(st, "voice_client", None)
        if vc is None or not vc.is_connected():
            return False
        player = getattr(st, "player", None)
        if player is None:
            continue
        if not player.is_playing():
            # Paused-by-logic stations flip listener_count to 0 before
            # is_paused, so a non-zero listener_count + not-playing really
            # means unexpected silence, not an intentional pause.
            if getattr(st, "is_paused", False):
                continue
            # Enforce 10s silence limit: track when this station went silent.
            # Store monotonic timestamp on the station itself (transient, not
            # persisted) — avoids extra global state and works per-station.
            attr = "_gatus_silence_since"
            since = getattr(st, attr, None)
            if since is None:
                # First silent sample — record start, but don't fail yet
                # (transient gap between tracks is <2s).
                with contextlib.suppress(Exception):
                    setattr(st, attr, cur)
                # If threshold is 0, fail immediately; otherwise wait.
                if silence_threshold <= 0:
                    return False
                continue
            if cur - float(since) >= silence_threshold:
                return False
        else:
            # Playing again — clear silence marker (delete only: the next
            # silent sample then takes the first-sample path, as intended).
            with contextlib.suppress(Exception):
                if hasattr(st, "_gatus_silence_since"):
                    delattr(st, "_gatus_silence_since")
    return True


class GatusHeartbeat:
    """Pushes voice-connection health to the Gatus external endpoint."""

    def __init__(
        self,
        *,
        push_url: str,
        push_token: str,
        interval_seconds: int | float = 5,
    ) -> None:
        self.push_url = push_url.rstrip("/")
        self.push_token = push_token
        # Guard against a misconfigured 0/negative interval busy-looping.
        # Captain: 5s cadence so a 10s silence threshold is caught promptly
        # (30s was too slow — 60s window was way too much).
        self.interval_seconds = interval_seconds if interval_seconds > 0 else 1
        if self.interval_seconds > 10:
            log.warning("gatus heartbeat interval %s >10s clamped to 5s for 10s silence SLA", self.interval_seconds)
            self.interval_seconds = 5
        self._headers = {"Authorization": f"Bearer {push_token}"}
        self._previous_healthy: bool | None = None

    async def push(
        self,
        client: HeartbeatHttpClient,
        *,
        success: bool,
        error: str | None = None,
    ) -> bool:
        """POST one heartbeat. Never raises; returns whether the push landed."""
        params: dict[str, str] = {"success": "true" if success else "false"}
        if error:
            params["error"] = error
        try:
            resp = await client.post(
                f"{self.push_url}{ENDPOINT_PATH}",
                params=params,
                headers=self._headers,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            # A failed push just means no heartbeat arrives — exactly the
            # condition Gatus alerts on. Log and move on, never raise.
            log.warning("gatus heartbeat push failed: %s", exc)
            return False

    async def tick(self, client: HeartbeatHttpClient, *, is_healthy: bool) -> bool:
        """Evaluate one health sample and push if the cadence demands it.

        Healthy: push ``success=true`` (every tick — this is the regular
        cadence Gatus measures). Unhealthy: push ``success=false`` once on the
        transition (or first sample), then stay silent until recovery so Gatus
        alerts via its heartbeat timeout rather than spamming the endpoint.
        """
        if is_healthy:
            ok = await self.push(client, success=True)
            self._previous_healthy = True
            return ok
        if self._previous_healthy is not False:
            ok = await self.push(client, success=False, error=UNHEALTHY_ERROR)
            self._previous_healthy = False
            return ok
        return True

    async def run(
        self,
        client: HeartbeatHttpClient,
        *,
        health_check: Callable[[], bool],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Background cadence loop; runs until cancelled.

        ``health_check`` is invoked every ``interval_seconds`` and must return
        the current radio health (e.g. a closure over ``bot.main``'s
        authoritative ``stations`` dict). ``sleep`` is injectable for
        deterministic tests.
        """
        try:
            while True:
                await sleep(self.interval_seconds)
                try:
                    is_healthy = bool(health_check())
                    await self.tick(client, is_healthy=is_healthy)
                except Exception:
                    log.warning("gatus heartbeat tick failed", exc_info=True)
        except asyncio.CancelledError:
            pass
