"""Discord bot entry point.

Wires together config + DB + provider client + player + tracker + scheduler
and hands control to discord.py's event loop.

Boot sequence (blueprint §7.1):
    1. Load config, open DB, open provider HTTP client.
    2. Connect to Discord, resolve guild + voice channel + text channel.
    3. Close any orphan sessions left from a previous crash.
    4. Join voice channel.
    5. Ask provider for `current()`; start playback at saved position.
    6. Register voice_state_update handler + start scheduler tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import httpx

from bot.config import BotConfig, load
from bot.milestones import MilestoneAnnouncer, NowPlaying
from bot.player import Player
from bot.presence import Transition, VoiceEvent, should_pause, should_resume
from bot.scheduler import Scheduler
from bot.state import BotState
from bot.tracker import SessionTracker
from db.database import Database
from provider.client import FileProviderClient, ProviderError

log = logging.getLogger(__name__)


def _init_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # discord.py is noisy at DEBUG; keep INFO.
    logging.getLogger("discord").setLevel(logging.INFO)


async def _resume_or_start(
    player: Player,
    provider: FileProviderClient,
    state: BotState,
    *,
    max_attempts: int = 20,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> None:
    """Kick off playback on startup (blueprint §7.1).

    Retries with exponential backoff — the file-provider container may still
    be scanning its playlist when the bot connects.
    """
    resume_id = state.current_track_id
    resume_at = state.playback_position_seconds
    backoff = initial_backoff

    for attempt in range(1, max_attempts + 1):
        try:
            if resume_id:
                track = await provider.get_by_id(resume_id)
                if not track.ready or not track.local_path:
                    log.warning("track %s not ready — falling back to /current", resume_id)
                    track = await provider.current()
                    resume_at = 0
                else:
                    log.info("resuming %s @ %ds", track.title, resume_at)
            else:
                track = await provider.current()
                log.info("starting playback: %s", track.title)
            await player.start(track, seek_seconds=resume_at)
            return
        except ProviderError as exc:
            log.warning(
                "startup provider not ready (attempt %d/%d): %s",
                attempt,
                max_attempts,
                exc,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

    log.error("provider unavailable after %d attempts — bot stays idle", max_attempts)


def _non_bot_members(channel, *, exclude_user_id: str | None = None) -> list:
    """Return the list of human members currently in `channel`.

    ``exclude_user_id`` is used from `on_voice_state_update`'s LEFT branch —
    discord.py's cached member list may still contain the departing user at
    handler-invocation time, so we filter them out explicitly to make the
    empty-channel check reliable.
    """
    return [
        m
        for m in channel.members
        if not m.bot and (exclude_user_id is None or str(m.id) != exclude_user_id)
    ]


async def run(config: BotConfig | None = None) -> None:  # pragma: no cover — I/O heavy
    """Main coroutine. Not covered by tests — validated by manual + Docker runs."""
    import discord

    config = config or load()
    _init_logging()

    db = Database(config.database_path)
    state = BotState(db)
    provider = FileProviderClient(config.file_provider_base_url)

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.members = (
        False  # voice_states is enough to track channel presence without privileged intent
    )
    intents.guilds = True
    intents.message_content = False  # we don't read messages

    client = discord.Client(intents=intents)

    tracker = SessionTracker(db=db, min_session_seconds=config.min_session_seconds)
    milestones = MilestoneAnnouncer(client=client, text_channel_id=config.text_channel_id, db=db)
    now_playing = NowPlaying(
        client=client, text_channel_id=config.text_channel_id, state=state, db=db
    )
    player: Player | None = None
    voice: object | None = None
    text_channel = None
    ready_done = False  # guard against on_ready firing more than once

    async def _handle_command(command: str, payload: dict | None) -> str:
        """Called by the scheduler's command loop for each pending row."""
        if player is None:
            return "error: player not ready"
        if command == "skip":
            await player.skip()
            return "ok:skipped"
        if command == "pause":
            await player.pause()
            return "ok:paused"
        if command == "resume":
            await player.resume()
            if player.current_track is not None:
                await now_playing.post_or_replace(player.current_track)
            return "ok:resumed"
        if command == "set_volume":
            try:
                volume = int((payload or {}).get("volume_percent"))
            except (TypeError, ValueError):
                return "error: set_volume requires integer payload {volume_percent}"
            if not 50 <= volume <= 250:
                return "error: volume must be between 50 and 250"
            applied = await player.set_volume(volume)
            return f"ok:volume:{applied}"
        if command == "play_track":
            if not payload or not isinstance(payload.get("track_id"), str):
                return "error: play_track requires payload {track_id}"
            track_id = payload["track_id"]
            try:
                track = await provider.jump_to(track_id)
            except Exception as exc:
                return f"error: jump failed: {exc}"
            if not track.ready or not track.local_path:
                return f"error: track {track_id} not ready"
            await player.start(track)
            with contextlib.suppress(Exception):
                await now_playing.post_or_replace(track)
            return f"ok:playing:{track_id}"
        if command == "refresh_playlist":
            # File-provider owns playlists — ask it to rescan.
            try:
                async with httpx.AsyncClient(base_url=config.file_provider_base_url) as h:
                    r = await h.post("/refresh", timeout=30)
                return f"ok:{r.status_code}"
            except Exception as exc:
                return f"error: {exc}"
        return f"error: unknown command {command!r}"

    scheduler = Scheduler(
        db=db,
        tracker=tracker,
        milestones=milestones,
        checkpoint_interval_seconds=config.checkpoint_interval_seconds,
        command_handler=_handle_command,
    )

    async def _advance_and_announce(_player: Player, finished_track) -> None:
        """on_finish: mark played, ask for next, start it, repost embed.

        Retries with backoff on provider failure — the alternative is the bot
        going silent forever after one transient error.
        """
        # Best-effort: mark_played is non-critical.
        with contextlib.suppress(Exception):
            await provider.mark_played(finished_track.track_id)

        backoff = 1.0
        for attempt in range(1, 11):
            try:
                nxt = await provider.next()
                if not nxt.ready or not nxt.local_path:
                    raise RuntimeError(f"track {nxt.track_id} not ready")
                await _player.start(nxt)
                with contextlib.suppress(Exception):
                    await now_playing.post_or_replace(nxt)
                return
            except Exception as exc:
                log.warning("advance attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        log.error("giving up on advancing after 10 attempts — bot will be silent")

    @client.event
    async def on_ready():
        nonlocal voice, player, text_channel, ready_done
        log.info("connected as %s", client.user)
        if ready_done:
            # discord.py may fire on_ready again after a session resume.
            # We only want to set up voice / scheduler / etc. once.
            log.info("on_ready fired again (reconnect); skipping re-init")
            return
        ready_done = True

        guild = client.get_guild(config.guild_id)
        if guild is None:
            log.error("guild %s not found — is the bot invited?", config.guild_id)
            await client.close()
            return

        vc_channel = guild.get_channel(config.voice_channel_id)
        if not isinstance(vc_channel, discord.VoiceChannel):
            log.error("voice channel %s is not a voice channel", config.voice_channel_id)
            await client.close()
            return

        text_channel = guild.get_channel(config.text_channel_id)
        if text_channel is None:
            log.warning(
                "text channel %s not found — announcements will be skipped", config.text_channel_id
            )

        # Close any sessions left open by a previous crash.
        closed = tracker.close_orphan_sessions()
        if closed:
            log.info("closed %d orphan sessions on startup", closed)

        # Connect to voice.
        voice = await vc_channel.connect(reconnect=True)

        # Build the player now that we have a live VoiceClient + running loop.
        loop = asyncio.get_running_loop()
        player = Player(
            voice_client=voice,
            provider=provider,
            state=state,
            loop=loop,
        )
        player.on_finish(_advance_and_announce)

        # If channel is already empty, don't start audio — just stay connected.
        listeners = _non_bot_members(vc_channel)
        if not listeners:
            log.info("voice channel is empty on startup — staying silent")
            state.is_paused = True
        else:
            # Open sessions for anyone already present.
            for member in listeners:
                tracker.open_session(
                    user_id=str(member.id),
                    username=str(member),
                    server_nickname=member.display_name,
                    track_id=state.current_track_id,
                )
            await _resume_or_start(player, provider, state)
            cur = player.current_track
            if cur is not None:
                await now_playing.post_or_replace(cur)

        scheduler.start()

    @client.event
    async def on_voice_state_update(member, before, after):
        if member.bot or member.guild.id != config.guild_id:
            return

        event = VoiceEvent(
            user_id=str(member.id),
            is_bot=member.bot,
            before_channel_id=before.channel.id if before.channel else None,
            after_channel_id=after.channel.id if after.channel else None,
        )
        transition = event.transition(config.voice_channel_id)

        if transition is Transition.JOINED:
            tracker.open_session(
                user_id=event.user_id,
                username=str(member),
                server_nickname=member.display_name,
                track_id=state.current_track_id,
            )
            listeners = _non_bot_members(after.channel)
            if player is not None and should_resume(len(listeners), state.is_paused):
                await player.resume()
                if player.current_track is not None:
                    await now_playing.post_or_replace(player.current_track)
            else:
                now_playing.trigger_watcher_count_update()
        elif transition is Transition.LEFT:
            closed = tracker.close_session(user_id=event.user_id)
            if closed is not None:
                await milestones.check_and_announce(closed.user_id)
            # Explicitly exclude the departing user — discord.py's member
            # cache may still include them at this point.
            listeners = _non_bot_members(before.channel, exclude_user_id=event.user_id)
            if player is not None and should_pause(len(listeners), state.is_paused):
                await player.pause()
            now_playing.trigger_watcher_count_update()

    # Graceful shutdown: close scheduler + provider + DB.
    def _install_signal_handlers() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    async def _shutdown() -> None:
        log.info("shutting down")
        with contextlib.suppress(Exception):
            scheduler.stop()
        with contextlib.suppress(Exception):
            if player is not None:
                await player.stop_hard()
        with contextlib.suppress(Exception):
            if voice is not None:
                await voice.disconnect(force=True)  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            await provider.aclose()
        with contextlib.suppress(Exception):
            db.close()
        with contextlib.suppress(Exception):
            await client.close()

    _install_signal_handlers()

    try:
        await client.start(config.token)
    finally:
        await _shutdown()


def main() -> None:  # pragma: no cover
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()
