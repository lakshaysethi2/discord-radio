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
) -> None:
    """Kick off playback on startup (blueprint §7.1)."""
    resume_id = state.current_track_id
    resume_at = state.playback_position_seconds
    try:
        if resume_id:
            track = await provider.get_by_id(resume_id)
            log.info("resuming %s @ %ds", track.title, resume_at)
            await player.start(track, seek_seconds=resume_at)
        else:
            track = await provider.current()
            log.info("starting playback: %s", track.title)
            await player.start(track)
    except ProviderError as exc:
        log.error("could not start playback: %s", exc)


def _non_bot_members(channel) -> list:
    """Return the list of human members currently in `channel`."""
    return [m for m in channel.members if not m.bot]


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
    intents.members = True  # to enumerate voice channel members
    intents.guilds = True
    intents.message_content = False  # we don't read messages

    client = discord.Client(intents=intents)

    tracker = SessionTracker(db=db, min_session_seconds=config.min_session_seconds)
    milestones = MilestoneAnnouncer(client=client, text_channel_id=config.text_channel_id, db=db)
    now_playing = NowPlaying(
        client=client, text_channel_id=config.text_channel_id, state=state, db=db
    )
    scheduler = Scheduler(
        db=db,
        tracker=tracker,
        milestones=milestones,
        checkpoint_interval_seconds=config.checkpoint_interval_seconds,
    )

    player: Player | None = None
    voice: object | None = None
    text_channel = None
    ready_done = False  # guard against on_ready firing more than once

    async def _advance_and_announce(_player: Player, finished_track) -> None:
        """on_finish: mark played, ask for next, start it, repost embed."""
        try:
            await provider.mark_played(finished_track.track_id)
            nxt = await provider.next()
            await now_playing.post_or_replace(nxt)
            await _player.start(nxt)
        except Exception:
            log.exception("failed to advance to next track")

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
        elif transition is Transition.LEFT:
            closed = tracker.close_session(user_id=event.user_id)
            if closed is not None:
                await milestones.check_and_announce(closed.user_id)
            listeners = _non_bot_members(before.channel)
            if player is not None and should_pause(len(listeners), state.is_paused):
                await player.pause()

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
