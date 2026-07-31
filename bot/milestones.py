"""Milestone detection + Now Playing message management (§8, §10).

Split into two classes:

* `MilestoneChecker` — pure logic: given the DB, decides which milestone flags
  should flip and returns a list of `Milestone` tuples. No discord.py imports.
* `MilestoneAnnouncer` — thin discord.py wrapper that calls the checker and
  posts to the text channel. Set the checker's `db` to a real Database and
  the announcer only needs `client` + `text_channel_id`.
* `NowPlaying` — manages the pinned/reposted embed for the current track.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from db.database import Database
from db.guilds import apply_guild_config, get_guild_config
from db.models import MILESTONES

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Milestone:
    user_id: str
    username: str
    hours: int
    column: str  # e.g. "milestone_5h"


class MilestoneChecker:
    """Pure milestone detection — no I/O beyond the DB."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def check_user(self, user_id: str) -> list[Milestone]:
        """Return milestones the user has newly reached (and mark them fired).

        Called after every session close and every checkpoint. If the user's
        alltime seconds crossed a threshold and the corresponding
        `milestone_Xh` flag is still 0, we set it to 1 in the same DB txn.
        """
        row = self.db.fetchone(
            "SELECT username, total_seconds_alltime, "
            "milestone_5h, milestone_10h, milestone_100h, milestone_1000h "
            "FROM user_totals WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return []

        hours = int(row["total_seconds_alltime"] or 0) // 3600
        earned: list[Milestone] = []
        updates: list[str] = []
        for threshold, col in MILESTONES:
            if hours >= threshold and not bool(row[col]):
                earned.append(
                    Milestone(
                        user_id=user_id, username=row["username"], hours=threshold, column=col
                    )
                )
                updates.append(col)
        if updates:
            set_clause = ", ".join(f"{c}=1" for c in updates)
            self.db.execute(f"UPDATE user_totals SET {set_clause} WHERE user_id=?", (user_id,))
        return earned


class MilestoneAnnouncer:
    """Discord-side wrapper: check + post to the configured text channel."""

    def __init__(
        self, *, client: Any, text_channel_id: int, db: Database, guild_id: str = ""
    ) -> None:
        self.client = client
        self.text_channel_id = text_channel_id
        self.db = db
        self.guild_id = guild_id
        self.checker = MilestoneChecker(db)

    async def check_and_announce(self, user_id: str) -> list[Milestone]:
        milestones = self.checker.check_user(user_id)
        if not milestones:
            return []
        channel = self.client.get_channel(self.text_channel_id)
        if channel is None:
            log.warning(
                "cannot announce milestones: text channel %s not found", self.text_channel_id
            )
            return milestones
        import discord

        for m in milestones:
            try:
                await channel.send(f"🎉 <@{m.user_id}> just reached **{m.hours} hours** watched!")
            except discord.Forbidden:
                log.warning(
                    "cannot announce milestones: missing access to text channel %s",
                    self.text_channel_id,
                )
                self.text_channel_id = None
                cfg = get_guild_config(self.db, self.guild_id)
                if cfg:
                    apply_guild_config(
                        self.db,
                        self.guild_id,
                        enabled=cfg.enabled,
                        voice_channel_id=cfg.voice_channel_id,
                        text_channel_id=None,
                    )
            except Exception:  # pragma: no cover — network flake
                log.exception("failed to announce milestone %s for %s", m.hours, user_id)
        return milestones


class NowPlaying:
    """Manages the 'Now Playing' embed: delete previous, post new, save id.

    ``guild_id`` scopes the "currently watching" count to this server (so each
    server's embed shows only its own listeners). Defaults to ``""`` which
    counts every open session globally — preserved for backward compatibility.
    """

    def __init__(
        self, *, client: Any, text_channel_id: int, state, db: Database, guild_id: str = ""
    ) -> None:
        self.client = client
        self.text_channel_id = text_channel_id
        self.state = state
        self.db = db
        self.guild_id = guild_id
        self._update_task: asyncio.Task | None = None

    def _fmt_duration(self, seconds: int) -> str:
        if seconds <= 0:
            return "—"
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}m"
        return f"{m}m {s:02d}s"

    def _watcher_count(self) -> int:
        if self.guild_id:
            row = self.db.fetchone(
                "SELECT COUNT(*) AS n FROM watch_sessions WHERE left_at IS NULL AND guild_id=?",
                (self.guild_id,),
            )
        else:
            row = self.db.fetchone("SELECT COUNT(*) AS n FROM watch_sessions WHERE left_at IS NULL")
        return int(row["n"]) if row else 0

    async def post_or_replace(self, track) -> None:  # pragma: no cover — discord I/O
        """Delete previous embed (if any), post a fresh one, remember its id."""
        import discord

        if self._update_task and not self._update_task.done():
            self._update_task.cancel()

        channel = self.client.get_channel(self.text_channel_id)
        if channel is None:
            log.warning("cannot post Now Playing: text channel not found")
            return

        prev_id = self.state.now_playing_message_id
        if prev_id:
            try:
                msg = await channel.fetch_message(prev_id)
                await msg.delete()
            except Exception:
                log.debug("could not delete previous Now Playing message %s", prev_id)

        # Playlist size — best-effort read (bot's DB doesn't have it, so leave blank).
        embed = discord.Embed(
            title="🎙️ Now Playing",
            description=f"**{track.title}**",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Duration", value=self._fmt_duration(track.duration_seconds))
        embed.add_field(name="Track", value=f"#{track.playlist_position + 1}")
        embed.add_field(name="Currently watching", value=f"👥 {self._watcher_count()}")

        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning(
                "cannot post Now Playing: missing access to text channel %s",
                self.text_channel_id,
            )
            self.text_channel_id = None
            cfg = get_guild_config(self.db, self.guild_id)
            if cfg:
                apply_guild_config(
                    self.db,
                    self.guild_id,
                    enabled=cfg.enabled,
                    voice_channel_id=cfg.voice_channel_id,
                    text_channel_id=None,
                )
            return
        except Exception:
            log.exception("could not post Now Playing embed")
            return
        self.state.now_playing_message_id = int(msg.id)

    async def update_watcher_count(self) -> None:
        """Edit the current Now Playing message to update the currently watching count."""
        import discord

        prev_id = self.state.now_playing_message_id
        if not prev_id:
            return
        channel = self.client.get_channel(self.text_channel_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(prev_id)
            if msg and msg.embeds:
                embed = msg.embeds[0]
                for idx, field in enumerate(embed.fields):
                    if field.name == "Currently watching":
                        embed.set_field_at(
                            idx,
                            name="Currently watching",
                            value=f"👥 {self._watcher_count()}",
                            inline=field.inline,
                        )
                        await msg.edit(embed=embed)
                        break
        except discord.Forbidden:
            log.warning(
                "cannot update Now Playing: missing access to text channel %s",
                self.text_channel_id,
            )
            self.text_channel_id = None
            cfg = get_guild_config(self.db, self.guild_id)
            if cfg:
                apply_guild_config(
                    self.db,
                    self.guild_id,
                    enabled=cfg.enabled,
                    voice_channel_id=cfg.voice_channel_id,
                    text_channel_id=None,
                )
        except Exception:
            log.debug("could not update watcher count on message %s", prev_id)

    def trigger_watcher_count_update(self) -> None:
        """Schedule a debounced watcher count update to avoid rate limits."""
        if self._update_task and not self._update_task.done():
            return  # update already scheduled

        async def _debounced():
            await asyncio.sleep(2.0)  # wait 2 seconds for transitions to settle
            await self.update_watcher_count()

        loop = None
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
        if loop:
            self._update_task = loop.create_task(_debounced())
