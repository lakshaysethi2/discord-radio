"""Discord bot entry point.

Wires together config + DB + provider client + player + tracker + scheduler
and hands control to discord.py's event loop.

Boot sequence (blueprint §7.1, extended for multi-server §servers):
    1. Load config, open DB, open provider HTTP client.
    2. Connect to Discord, discover every guild we belong to.
    3. Close any orphan sessions left from a previous crash.
    4. For each *enabled* guild with valid channels: join its voice channel,
       build a Station (player + Now Playing + milestone announcer).
    5. Ask provider for `current()`; start playback at the saved position.
    6. Register voice_state_update handler + start scheduler tasks.

The radio has ONE global playback cursor — every enabled server hears the same
track — but each server keeps its own voice connection, Now Playing embed and
milestone announcements.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bot.commands import build_commands
from bot.config import BotConfig, load
from bot.milestones import MilestoneAnnouncer, NowPlaying
from bot.player import Player
from bot.presence import Transition, VoiceEvent, should_pause, should_resume
from bot.scheduler import Scheduler
from bot.state import BotState, GuildScopedState
from bot.tracker import SessionTracker
from db import guilds as guilds_db
from db.database import Database
from provider.client import FileProviderClient, ProviderError

log = logging.getLogger(__name__)


@dataclass
class Station:
    """One Discord server the bot is actively serving.

    Every enabled guild gets its own voice connection, player, Now Playing
    embed and milestone announcer — but they all share the single global
    playback cursor (the radio plays the same track everywhere).
    """

    guild_id: str
    guild_name: str
    voice_channel: object
    voice_channel_id: int
    text_channel_id: int | None
    voice_client: object
    player: Player
    now_playing: NowPlaying
    milestones: MilestoneAnnouncer
    is_paused: bool = True
    listener_count: int = 0


class RadioClock:
    """Authoritative shared-radio playback clock (independent of any voice client).

    The radio plays one stream into many servers, so the "current position"
    must not live on a single Player -- a player that joined late (or an idle
    one that paused) would otherwise overwrite the global position with its
    own stale clock. RadioClock owns the single source of truth:

    * while playing: position = base_offset + (now - started_at)
    * while paused:   position = base_offset (frozen)

    Stations start/resume by seeking to radio.position() so every server joins
    the stream at exactly the same point.
    """

    def __init__(self) -> None:
        self._playing = False
        self._started_at: float | None = None
        self._base_offset = 0.0

    def init_from_state(self, offset: float, playing: bool) -> None:
        self._base_offset = max(0.0, float(offset))
        self._playing = bool(playing)
        self._started_at = time.monotonic() if playing else None

    def start(self, base_offset: float) -> None:
        self._playing = True
        self._started_at = time.monotonic()
        self._base_offset = max(0.0, float(base_offset))

    def reset(self, offset: float = 0.0) -> None:
        """Set the cursor to a frozen ``offset`` without starting the clock.

        Used when an explicit track is selected while nobody is listening: the
        shared cursor is parked at the start of the chosen track, but the clock
        stays paused until `sync_radio_state()` decides playback should run.
        """
        self._base_offset = max(0.0, float(offset))
        self._playing = False
        self._started_at = None

    def pause(self) -> None:
        if self._playing:
            # Freeze at the exact position we are currently at.
            self._base_offset = self.position()
        self._playing = False
        self._started_at = None

    def position(self) -> float:
        if self._playing and self._started_at is not None:
            return self._base_offset + max(0.0, time.monotonic() - self._started_at)
        return self._base_offset

    def is_playing(self) -> bool:
        return self._playing


async def resume_station_at_radio_position(
    player, provider, state, radio, *, max_attempts: int = 3
) -> object | None:
    """Start a station's player at the *shared* radio position.

    Used when the first listener joins a previously-idle station (or when a
    resume command is issued): the station must pick up the same track at the
    same offset everyone else is hearing, not a stale persisted position.
    """
    track_id = state.current_track_id
    if not track_id:
        return None
    seek = radio.position()
    track = None
    for _ in range(max(1, max_attempts)):
        try:
            track = await provider.get_by_id(track_id)
        except Exception:
            track = None
            break
        if track.ready and track.local_path:
            break
        # Not ready yet; the advance loop owns the heavier retry logic.
        break
    if track is None or not track.ready or not track.local_path:
        return None
    await player.start(track, seek_seconds=seek)
    return track


def sync_radio_state(
    stations: dict[str, Station],
    radio: RadioClock,
    state: BotState,
    *,
    admin_paused: bool,
) -> bool:
    """Compute and apply the effective shared-radio state.

    The radio plays only when at least one server has listeners AND no manual
    admin (dashboard) pause is in effect. This keeps the authoritative
    ``RadioClock`` frozen whenever playback is effectively stopped — whether
    because the last listener left or because an admin pressed pause — and
    resumes it from the exact frozen offset when playback should continue.

    ``admin_paused`` is the manual pause flag owned by the orchestrator; it is
    *not* cleared by listener changes, so an admin pause survives people
    joining/leaving. Returns ``True`` if the radio is now playing, else
    ``False``.
    """
    has_listeners = any(s.listener_count > 0 for s in stations.values())
    should_play = has_listeners and not admin_paused
    if should_play and not radio.is_playing():
        # Resume the clock from wherever it was frozen — every station will
        # re-join the stream at this same offset.
        radio.start(radio.position())
    elif not should_play and radio.is_playing():
        # Freeze the clock at the current position.
        radio.pause()
    state.is_paused = not should_play
    if not should_play:
        state.playback_position_seconds = int(radio.position())
    return should_play


async def apply_server_config(
    *,
    db: Database,
    client: object,
    stations: dict[str, Station],
    per_guild_announcers: dict[str, MilestoneAnnouncer],
    build_station: Callable[[object, object], Awaitable[Station | None]],
    teardown_station: Callable[[Station], Awaitable[None]],
    guild_id: str,
    get_guild_config=guilds_db.get_guild_config,
) -> str:
    """Reconcile one guild's live station with its current DB config.

    Called from the control-plane ``apply_server`` command so a dashboard
    save takes effect immediately (connect / disconnect / re-point channels)
    without restarting the bot. It is idempotent — if the live station
    already matches the saved config it is left untouched.

    ``build_station`` / ``teardown_station`` are injected by the running bot
    (they wrap discord.py I/O), which keeps this function free of Discord
    dependencies and straightforward to unit-test with fakes.
    """

    def _unregister(gid: str) -> None:
        stations.pop(gid, None)
        per_guild_announcers.pop(gid, None)

    cfg = get_guild_config(db, guild_id)
    guild = None
    if guild_id:
        with contextlib.suppress(Exception):
            guild = client.get_guild(int(guild_id))
    existing = stations.get(guild_id)

    wants_on = (
        cfg is not None
        and cfg.enabled
        and cfg.voice_channel_id is not None
        and cfg.text_channel_id is not None
    )

    if not wants_on:
        if existing is not None:
            await teardown_station(existing)
            _unregister(guild_id)
            log.info("guild %s disabled — live station torn down", guild_id)
        return "ok:disabled"

    # --- Server should be live. ---
    if existing is not None:
        if str(existing.voice_channel_id) != str(cfg.voice_channel_id):
            # Voice channel changed → reconnect from scratch.
            await teardown_station(existing)
            _unregister(guild_id)
            existing = None
        elif str(existing.text_channel_id) != str(cfg.text_channel_id):
            # Only the *Now Playing* text channel changed — repoint the
            # announcers in place, no voice reconnect required.
            tid = int(cfg.text_channel_id)
            existing.text_channel_id = tid
            existing.now_playing.text_channel_id = tid
            existing.milestones.text_channel_id = tid
            if ann := per_guild_announcers.get(guild_id):
                ann.text_channel_id = tid
            log.info("guild %s: Now Playing channel updated to %s", guild_id, tid)
            return "ok:text_channel_updated"

    if existing is None:
        if guild is None:
            log.warning("guild %s not found — is the bot still invited?", guild_id)
            return f"error: guild {guild_id} not found"
        station = await build_station(guild, cfg)
        if station is None:
            return "error: voice connect failed"
        stations[guild_id] = station
        per_guild_announcers[guild_id] = station.milestones
        log.info("guild %s enabled — live station built and joined", guild_id)

    return "ok:applied"


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
    """Main coroutine. Not covered by tests — validated by manual + Docker runs.

    The bot serves every *enabled* guild discovered in ``guild_configs`` (seeded
    from the servers it actually belongs to + the legacy env vars). Each guild
    gets its own voice connection + Now Playing embed + milestone announcer,
    but they all share one global playback cursor — the same radio, everywhere.
    """
    import discord
    from discord import app_commands

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
    tree = app_commands.CommandTree(client)

    tracker = SessionTracker(db=db, min_session_seconds=config.min_session_seconds)

    # The running event loop. Defined here (not inside `on_ready`) so the
    # nested `_build_station` can close over it when live-applying a server.
    loop = asyncio.get_running_loop()

    # Populated in on_ready; referenced by the event handlers below.
    stations: dict[str, Station] = {}
    per_guild_announcers: dict[str, MilestoneAnnouncer] = {}
    _advance_lock = asyncio.Lock()
    # The single authoritative shared-radio clock (see RadioClock).
    radio = RadioClock()
    ready_done = False  # guard against on_ready firing more than once
    admin_paused = False  # manual dashboard pause; independent of listeners
    slash_commands_registered = False  # guard against double-registration on reconnect

    async def _handle_command(command: str, payload: dict | None) -> str:
        """Called by the scheduler's command loop for each pending row.

        Controls act on the shared stream, so we fan them out to every station
        that currently has listeners.
        """
        nonlocal admin_paused
        # `apply_server` is the one command whose whole job is to *create*
        # stations, so it must run even when `stations` is empty (e.g. right
        # after a fresh, idle startup). Handle it before the "no servers"
        # guard below, otherwise a brand-new idle bot could never be enabled
        # from the dashboard without a restart.
        if command == "apply_server":
            # Dashboard pushed a new/changed server config — apply it live so
            # the admin doesn't have to restart the bot.
            gid = (payload or {}).get("guild_id")
            if not gid:
                return "error: apply_server requires payload {guild_id}"
            result = await apply_server_config(
                db=db,
                client=client,
                stations=stations,
                per_guild_announcers=per_guild_announcers,
                build_station=_build_station,
                teardown_station=_teardown_station,
                guild_id=gid,
            )
            # Keep the shared radio cursor in step with the new station set
            # (e.g. a disable should freeze the clock if it was the only one).
            sync_radio_state(stations, radio, state, admin_paused=admin_paused)
            return result
        if command == "refresh_playlist":
            # File-provider owns playlists — ask it to rescan.
            try:
                items = (payload or {}).get("archive_org_items") if payload else None
                if not items:
                    from db.models import BotStateKey

                    items = db.get_state(BotStateKey.ARCHIVE_ORG_ITEMS)
                res = await provider.refresh(archive_org_items=items)
                return f"ok:{res}"
            except Exception as exc:
                return f"error: {exc}"
        if not stations:
            return "error: no servers configured"
        if command == "skip":
            for st in stations.values():
                if st.listener_count > 0:
                    await st.player.skip()
            return "ok:skipped"
        if command == "pause":
            admin_paused = True
            for st in stations.values():
                if st.listener_count > 0:
                    await st.player.pause()
                    st.is_paused = True
            # Freeze the shared clock so time does not advance while paused.
            sync_radio_state(stations, radio, state, admin_paused=admin_paused)
            return "ok:paused"
        if command == "resume":
            admin_paused = False
            # Resume the shared clock first (from its frozen offset); stations
            # then re-join the stream at the exact same position.
            sync_radio_state(stations, radio, state, admin_paused=admin_paused)
            for st in stations.values():
                if st.listener_count > 0:
                    await resume_station_at_radio_position(st.player, provider, state, radio)
                    st.is_paused = False
                    if st.player.current_track is not None:
                        await st.now_playing.post_or_replace(st.player.current_track)
            return "ok:resumed"
        if command == "set_volume":
            try:
                volume = int((payload or {}).get("volume_percent"))
            except (TypeError, ValueError):
                return "error: set_volume requires integer payload {volume_percent}"
            if not 50 <= volume <= 250:
                return "error: volume must be between 50 and 250"
            applied = volume
            for st in stations.values():
                applied = await st.player.set_volume(volume)
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
            # Playing a chosen track is an explicit play intent: clear any
            # manual pause, reset the shared cursor to offset 0, then reconcile
            # the effective radio state. If no listeners exist the clock stays
            # frozen at 0 (via sync_radio_state) until someone joins — it must
            # not start advancing in the background while nobody is listening.
            admin_paused = False
            radio.reset(0)
            state.playback_position_seconds = 0
            for st in stations.values():
                if st.listener_count > 0:
                    await st.player.start(track)
                with contextlib.suppress(Exception):
                    await st.now_playing.post_or_replace(track)
            sync_radio_state(stations, radio, state, admin_paused=admin_paused)
            return f"ok:playing:{track_id}"
        return f"error: unknown command {command!r}"

    async def _build_station(guild, cfg) -> Station | None:
        """Connect to a guild's configured voice channel and build a Station.

        Returns the (listener-bootstrapped) ``Station`` or ``None`` if the
        voice connection can't be established. The caller owns registration in
        ``stations`` / ``per_guild_announcers`` so startup and live-apply share
        a single registration point.
        """
        vc = guild.get_channel(int(cfg.voice_channel_id)) if cfg.voice_channel_id else None
        if not isinstance(vc, discord.VoiceChannel):
            log.warning(
                "guild %s: voice channel %s missing/invalid — cannot start",
                cfg.guild_id,
                cfg.voice_channel_id,
            )
            return None
        tc_id = int(cfg.text_channel_id) if cfg.text_channel_id else None

        # Bounded retry — same as startup; a UDP-discovery timeout is reported
        # once and fails, so retry a few times to ride out transient blips.
        voice_client = None
        for attempt in range(1, 4):
            try:
                voice_client = await vc.connect(reconnect=True, timeout=30.0)
                break
            except Exception as exc:  # pragma: no cover — network/permission edge case
                log.warning(
                    "guild %s: voice connect attempt %d/3 failed: %s",
                    cfg.guild_id,
                    attempt,
                    exc,
                )
        if voice_client is None:
            log.warning(
                "guild %s: giving up on voice connection — bot will not serve "
                "this server. Confirm Connect + Speak perms and UDP egress, "
                "then re-apply the server in the dashboard.",
                cfg.guild_id,
            )
            return None

        player = Player(
            voice_client=voice_client,
            provider=provider,
            state=state,
            loop=loop,
            persist_pause_state=False,
        )
        np_state = GuildScopedState(db, cfg.guild_id)
        now_playing = NowPlaying(
            client=client,
            text_channel_id=tc_id,
            state=np_state,
            db=db,
            guild_id=cfg.guild_id,
        )
        announcer = MilestoneAnnouncer(
            client=client, text_channel_id=tc_id, db=db, guild_id=cfg.guild_id
        )
        station = Station(
            guild_id=cfg.guild_id,
            guild_name=cfg.guild_name or guild.name,
            voice_channel=vc,
            voice_channel_id=int(cfg.voice_channel_id),
            text_channel_id=tc_id,
            voice_client=voice_client,
            player=player,
            now_playing=now_playing,
            milestones=announcer,
        )
        player.on_finish(_advance_and_announce)
        listeners = _non_bot_members(vc)
        station.listener_count = len(listeners)
        if listeners:
            station.is_paused = False
            for member in listeners:
                tracker.open_session(
                    guild_id=station.guild_id,
                    user_id=str(member.id),
                    username=str(member),
                    server_nickname=member.display_name,
                    track_id=state.current_track_id,
                )
            await _resume_or_start(player, provider, state)
            cur = player.current_track
            if cur is not None:
                await now_playing.post_or_replace(cur)
        else:
            station.is_paused = True
            log.info("guild %s voice channel empty on startup — staying silent", cfg.guild_id)
        return station

    async def _teardown_station(station: Station) -> None:
        """Stop playback and disconnect a live station (disable / rebuild)."""
        with contextlib.suppress(Exception):
            await station.player.stop_hard()
        with contextlib.suppress(Exception):
            await station.voice_client.disconnect(force=True)  # type: ignore[attr-defined]

    scheduler = Scheduler(
        db=db,
        tracker=tracker,
        per_guild_announcers=per_guild_announcers,
        checkpoint_interval_seconds=config.checkpoint_interval_seconds,
        command_handler=_handle_command,
    )

    async def _advance_and_announce(_player: Player, finished_track) -> None:
        """on_finish: mark played, ask for next, start it on every live station.

        The radio has one cursor, so the first station to finish a track drives
        the global advance and the others get cut to the new track (kept in
        sync). The lock + cursor check prevent a double advance when several
        stations finish the same track within the same instant.
        """
        async with _advance_lock:
            # Another server may have already advanced past this track.
            if state.current_track_id != finished_track.track_id:
                return

            # Best-effort: mark_played is non-critical.
            with contextlib.suppress(Exception):
                await provider.mark_played(finished_track.track_id)

            backoff = 1.0
            nxt = None
            for attempt in range(1, 11):
                try:
                    cand = await provider.next()
                    if not cand.ready or not cand.local_path:
                        raise RuntimeError(f"track {cand.track_id} not ready")
                    nxt = cand
                    break
                except Exception as exc:
                    log.warning("advance attempt %d failed: %s", attempt, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
            if nxt is None:
                log.error("giving up on advancing after 10 attempts — bot will be silent")
                return

            # The cursor moved to a new track starting at offset 0. Don't
            # unconditionally start the clock — if this on_finish fired in the
            # same instant the last listener left, ticking would advance the
            # shared radio in the background with nobody listening. Park at 0
            # and let sync_radio_state() decide whether playback should run
            # (same pattern as play_track).
            radio.reset(0)
            state.playback_position_seconds = 0
            sync_radio_state(stations, radio, state, admin_paused=admin_paused)
            for st in stations.values():
                try:
                    if st.listener_count > 0:
                        await st.player.start(nxt)
                    await st.now_playing.post_or_replace(nxt)
                except Exception as exc:
                    log.warning("station %s advance failed: %s", st.guild_id, exc)

    @client.event
    async def on_ready() -> None:
        nonlocal ready_done
        log.info("connected as %s", client.user)
        if ready_done:
            # discord.py may fire on_ready again after a session resume.
            # We only want to set up voice / scheduler / etc. once.
            log.info("on_ready fired again (reconnect); skipping re-init")
            return
        ready_done = True

        # 1. Discover every server we belong to + cache its channels so the
        #    dashboard can render <select> dropdowns without calling Discord.
        for guild in client.guilds:
            gid = str(guild.id)
            try:
                channels = await guild.fetch_channels()
            except Exception as exc:  # pragma: no cover — network edge case
                log.warning("could not fetch channels for guild %s: %s", gid, exc)
                channels = []
            guilds_db.discover_guild(db, gid, guild.name)
            ch_rows = []
            for c in channels:
                if isinstance(c, discord.VoiceChannel):
                    ctype = "voice"
                elif isinstance(c, discord.TextChannel):
                    ctype = "text"
                else:
                    continue
                ch_rows.append(
                    guilds_db.ChannelRow(
                        guild_id=gid,
                        channel_id=str(c.id),
                        channel_name=c.name,
                        channel_type=ctype,
                        # Discord nests a voice channel's text chat under the
                        # voice channel; capture parent_id so we can default
                        # *Now Playing* posts to that chat.
                        parent_id=str(c.category_id) if c.category_id else None,
                    )
                )
            guilds_db.replace_guild_channels(db, gid, ch_rows)

        # 2. Seed the legacy single-guild env vars once (if not admin-managed).
        guilds_db.seed_env_guild(db, config)

        # 3. Clean up any sessions left open by a previous crash *before* we
        #    open fresh ones for the people already in voice.
        closed = tracker.close_orphan_sessions()
        if closed:
            log.info("closed %d orphan sessions on startup", closed)

        # 4. Build a Station for every enabled guild with valid channels.
        voice_connect_failures = 0
        for cfg in guilds_db.get_enabled_guild_configs(db):
            guild = client.get_guild(int(cfg.guild_id))
            if guild is None:
                log.warning("guild %s not found — is the bot still invited?", cfg.guild_id)
                continue
            station = await _build_station(guild, cfg)
            if station is None:
                voice_connect_failures += 1
                continue
            stations[cfg.guild_id] = station
            per_guild_announcers[cfg.guild_id] = station.milestones

        if not stations:
            if voice_connect_failures:
                log.warning(
                    "bot is idle: %d enabled server(s) were found and valid but the "
                    "voice connection failed (see warnings above). Confirm the bot "
                    "role has Connect + Speak in the voice channel and that this host "
                    "can reach Discord's voice servers over UDP, then restart.",
                    voice_connect_failures,
                )
            else:
                log.warning(
                    "no enabled servers with valid channels — bot is idle. "
                    "Enable a server + pick channels in the dashboard."
                )

        # Initialise the shared-radio clock from the persisted cursor.
        total = sum(s.listener_count for s in stations.values())
        state.is_paused = total == 0
        radio.init_from_state(float(state.playback_position_seconds), playing=not state.is_paused)
        scheduler.start()

        # Register slash commands once, then sync globally.
        nonlocal slash_commands_registered
        if not slash_commands_registered:
            slash_commands_registered = True
            for name, desc, cb in build_commands(
                db=db, provider=provider, state=state, radio=radio, stations=stations
            ):
                tree.command(name=name, description=desc)(cb)
            try:
                synced = await tree.sync()
                log.info("synced %d slash commands globally", len(synced))
            except Exception:
                log.exception("failed to sync slash commands")

    @client.event
    async def on_voice_state_update(member, before, after):
        if member.bot:
            return
        station = stations.get(str(member.guild.id))
        if station is None:
            # Not a server we manage — ignore.
            return

        event = VoiceEvent(
            user_id=str(member.id),
            is_bot=member.bot,
            before_channel_id=before.channel.id if before.channel else None,
            after_channel_id=after.channel.id if after.channel else None,
        )
        transition = event.transition(station.voice_channel_id)

        if transition is Transition.JOINED:
            tracker.open_session(
                guild_id=station.guild_id,
                user_id=event.user_id,
                username=str(member),
                server_nickname=member.display_name,
                track_id=state.current_track_id,
            )
            listeners = _non_bot_members(after.channel) if after.channel else []
            station.listener_count = len(listeners)
            if not admin_paused and should_resume(len(listeners), station.is_paused):
                # Join the shared radio *at its current position*, not at a
                # stale per-player clock — every server hears the same offset.
                await resume_station_at_radio_position(station.player, provider, state, radio)
                station.is_paused = False
                cur = station.player.current_track
                if cur is not None:
                    await station.now_playing.post_or_replace(cur)
            else:
                station.now_playing.trigger_watcher_count_update()
        elif transition is Transition.LEFT:
            closed = tracker.close_session(guild_id=station.guild_id, user_id=event.user_id)
            if closed is not None:
                await station.milestones.check_and_announce(closed.user_id)
            # Explicitly exclude the departing user — discord.py's member
            # cache may still include them at this point.
            listeners = (
                _non_bot_members(before.channel, exclude_user_id=event.user_id)
                if before.channel
                else []
            )
            station.listener_count = len(listeners)
            if should_pause(len(listeners), station.is_paused):
                await station.player.pause()
                station.is_paused = True
            station.now_playing.trigger_watcher_count_update()

        # Keep the shared clock frozen/paused in step with listeners + any
        # manual admin pause (does not clear the manual-pause flag).
        sync_radio_state(stations, radio, state, admin_paused=admin_paused)

    # Graceful shutdown: close scheduler + provider + DB.
    def _install_signal_handlers() -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    async def _shutdown() -> None:
        log.info("shutting down")
        with contextlib.suppress(Exception):
            scheduler.stop()
        for st in stations.values():
            with contextlib.suppress(Exception):
                await st.player.stop_hard()
            with contextlib.suppress(Exception):
                await st.voice_client.disconnect(force=True)  # type: ignore[attr-defined]
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
