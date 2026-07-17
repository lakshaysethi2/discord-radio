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

import logging
from dataclasses import dataclass
from typing import Any

from db.database import Database
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

    def __init__(self, *, client: Any, text_channel_id: int, db: Database) -> None:
        self.client = client
        self.text_channel_id = text_channel_id
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
        for m in milestones:
            try:
                await channel.send(f"🎉 <@{m.user_id}> just reached **{m.hours} hours** watched!")
            except Exception:  # pragma: no cover — network flake
                log.exception("failed to announce milestone %s for %s", m.hours, user_id)
        return milestones


class NowPlaying:
    """Manages the 'Now Playing' embed: delete previous, post new, save id."""

    def __init__(self, *, client: Any, text_channel_id: int, state, db: Database) -> None:
        self.client = client
        self.text_channel_id = text_channel_id
        self.state = state
        self.db = db

    def _fmt_duration(self, seconds: int) -> str:
        if seconds <= 0:
            return "—"
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}m"
        return f"{m}m {s:02d}s"

    def _watcher_count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM watch_sessions WHERE left_at IS NULL")
        return int(row["n"]) if row else 0

    async def post_or_replace(self, track) -> None:  # pragma: no cover — discord I/O
        """Delete previous embed (if any), post a fresh one, remember its id."""
        import discord

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
        except Exception:
            log.exception("could not post Now Playing embed")
            return
        self.state.now_playing_message_id = int(msg.id)
