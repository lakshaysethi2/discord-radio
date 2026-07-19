"""Discord slash command definitions for the radio bot.

Commands:
    /current     — Show the currently playing track (public, in-channel).
    /next        — Skip to the next track in the queue.
    /leaderboard — Show listening leaderboard (ephemeral, only visible to caller).

Design:
    Commands are defined as pure factory functions that take dependencies
    (DB, provider, state, radio, stations) and return a list of callables
    ready for registration with a discord.py CommandTree. This keeps the
    commands testable without a live Discord connection.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.state import BotState
from dashboard import queries
from db.database import Database
from provider.client import FileProviderClient

log = logging.getLogger(__name__)


def _fmt_duration(seconds: int) -> str:
    """Human-readable duration string."""
    if seconds <= 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _watcher_count(db: Database, guild_id: str) -> int:
    """Count open watch sessions scoped to a guild."""
    if guild_id:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM watch_sessions WHERE left_at IS NULL AND guild_id=?",
            (guild_id,),
        )
    else:
        row = db.fetchone("SELECT COUNT(*) AS n FROM watch_sessions WHERE left_at IS NULL")
    return int(row["n"]) if row else 0


def build_commands(
    *,
    db: Database,
    provider: FileProviderClient,
    state: BotState,
    radio: Any,  # RadioClock
    stations: dict[str, Any],
) -> list[tuple[str, str, Any]]:
    """Return (name, description, callback) tuples for slash-command registration.

    The callbacks are async functions that accept a ``discord.Interaction``.
    """

    async def current_command(interaction) -> None:
        """Handle /current — show the currently playing track."""
        import discord

        guild_id = str(interaction.guild_id) if interaction.guild_id else ""

        # Gather track info from provider + state.
        track_id = state.current_track_id
        if not track_id:
            await interaction.response.send_message(
                "🎙️ Nothing is playing right now.", ephemeral=True
            )
            return

        try:
            track = await provider.get_by_id(track_id)
        except Exception:
            log.exception("failed to fetch current track from provider")
            await interaction.response.send_message(
                "⚠️ Could not reach the file provider. Try again in a moment.", ephemeral=True
            )
            return

        # Compute current playback position from the shared radio clock.
        pos_seconds = int(radio.position())
        paused = state.is_paused
        watchers = _watcher_count(db, guild_id)

        embed = discord.Embed(
            title="🎙️ Now Playing",
            description=f"**{track.title}**",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Duration", value=_fmt_duration(track.duration_seconds))
        embed.add_field(
            name="Progress",
            value=f"{_fmt_duration(pos_seconds)} / {_fmt_duration(track.duration_seconds)}"
            + (" ⏸️" if paused else ""),
        )
        embed.add_field(name="Track #", value=f"{track.playlist_position + 1}")
        embed.add_field(name="Currently watching", value=f"👥 {watchers}")

        await interaction.response.send_message(embed=embed)

    async def leaderboard_command(interaction) -> None:
        """Handle /leaderboard — show listening leaderboard (ephemeral)."""
        import discord

        rows = queries.leaderboard(db, period="alltime", limit=10)
        if not rows:
            await interaction.response.send_message(
                "📊 No listening data yet. Be the first to tune in!", ephemeral=True
            )
            return

        lines: list[str] = []
        for r in rows:
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r.rank, "")
            name = r.server_nickname or r.username
            # Escape markdown characters in names to prevent formatting abuse
            name = discord.utils.escape_markdown(name)
            lines.append(
                f"{medal} **#{r.rank}** {name} — {queries.format_hms(r.seconds)}"
            )

        embed = discord.Embed(
            title="📊 Listening Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text="All-time total listening time")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def next_command(interaction) -> None:
        """Handle /next — skip to the next track."""
        # Find stations in this guild that have listeners.
        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        active_stations = [
            s for s in stations.values()
            if s.guild_id == guild_id and s.listener_count > 0
        ]
        if not active_stations:
            await interaction.response.send_message(
                "⏭️ No active listeners in this server to skip for.", ephemeral=True
            )
            return

        for st in active_stations:
            await st.player.skip()

        await interaction.response.send_message("⏭️ Skipping to the next track…")

    return [
        ("current", "Show the currently playing track", current_command),
        ("next", "Skip to the next track in the queue", next_command),
        ("leaderboard", "Show listening time leaderboard", leaderboard_command),
    ]
