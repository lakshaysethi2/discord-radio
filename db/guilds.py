"""Per-guild configuration + cached channel discovery (§servers).

Admins use the dashboard to decide, per Discord server the bot is in:

* whether the bot is allowed to speak (``enabled``),
* which voice channel it joins,
* which text channel it posts *Now Playing* + milestones to.

The bot seeds this table from the servers it actually belongs to (see
``bot.main`` discovery) and, on first boot, from the legacy single-guild
environment variables. Everything here is pure SQL over the shared bot DB —
no Discord I/O — so it's trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from db.database import Database


@dataclass(slots=True)
class GuildConfig:
    guild_id: str
    guild_name: str | None
    enabled: bool
    voice_channel_id: str | None
    text_channel_id: str | None
    updated_at: str | None = None


@dataclass(slots=True)
class ChannelRow:
    guild_id: str
    channel_id: str
    channel_name: str | None
    channel_type: str  # 'voice' | 'text' | ...
    parent_id: str | None = None  # for a text chat nested under a voice channel


# ------------------------------------------------------------------- discovery
def discover_guild(db: Database, guild_id: str, guild_name: str | None) -> None:
    """Record that the bot belongs to ``guild_id``.

    Inserts a row if absent (disabled by default) and refreshes the display
    name on every sighting. Admin-chosen ``enabled`` / channel ids are *never*
    overwritten here — that's ``apply_guild_config``'s job.
    """
    db.execute(
        "INSERT INTO guild_configs(guild_id, guild_name, enabled) VALUES(?, ?, 0) "
        "ON CONFLICT(guild_id) DO UPDATE SET guild_name=excluded.guild_name",
        (guild_id, guild_name),
    )


def replace_guild_channels(db: Database, guild_id: str, channels: Iterable[ChannelRow]) -> None:
    """Atomically replace the cached channel list for one guild."""
    with db.transaction() as cur:
        cur.execute("DELETE FROM guild_channels WHERE guild_id=?", (guild_id,))
        for ch in channels:
            cur.execute(
                "INSERT OR REPLACE INTO guild_channels"
                "(guild_id, channel_id, channel_name, channel_type, parent_id) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    ch.guild_id,
                    ch.channel_id,
                    ch.channel_name,
                    ch.channel_type,
                    ch.parent_id or None,
                ),
            )


# ------------------------------------------------------------- admin writes
def apply_guild_config(
    db: Database,
    guild_id: str,
    *,
    enabled: bool,
    voice_channel_id: str | None,
    text_channel_id: str | None,
) -> None:
    """Persist an admin's per-server choices (dashboard save)."""
    db.execute(
        "INSERT INTO guild_configs"
        "(guild_id, enabled, voice_channel_id, text_channel_id, updated_at) "
        "VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "enabled=excluded.enabled, "
        "voice_channel_id=excluded.voice_channel_id, "
        "text_channel_id=excluded.text_channel_id, "
        "updated_at=CURRENT_TIMESTAMP",
        (guild_id, bool(enabled), voice_channel_id or None, text_channel_id or None),
    )


def seed_env_guild(db: Database, config: object) -> None:
    """Bootstrap the legacy single-guild env vars into ``guild_configs``.

    Called once on startup. We only seed when the env guild has *no*
    admin-managed config yet, and only if both configured channel ids are
    actually present in the channels we discovered for that guild (so we never
    point the bot at a channel it can't see).
    """
    guild_id = getattr(config, "guild_id", None)
    if not guild_id:
        return
    gid = str(guild_id)
    existing = get_guild_config(db, gid)
    if existing and (existing.voice_channel_id or existing.text_channel_id):
        # An admin has already taken ownership of this guild — don't clobber.
        return
    vcid = getattr(config, "voice_channel_id", None)
    tcid = getattr(config, "text_channel_id", None)
    vcid = str(vcid) if vcid else None
    tcid = str(tcid) if tcid else None
    if not vcid:
        return
    # If no explicit text channel was configured, default to the voice
    # channel's own text chat (Discord nests one there with "text in voice").
    if not tcid:
        tcid = get_associated_text_channel(db, gid, vcid)
    known = {ch.channel_id: ch.channel_type for ch in get_guild_channels(db, gid)}
    if vcid in known and (tcid is None or tcid in known):
        apply_guild_config(db, gid, enabled=True, voice_channel_id=vcid, text_channel_id=tcid)


# --------------------------------------------------------------------- reads
def _row(r) -> GuildConfig:
    return GuildConfig(
        guild_id=r["guild_id"],
        guild_name=r["guild_name"],
        enabled=bool(r["enabled"]),
        voice_channel_id=r["voice_channel_id"],
        text_channel_id=r["text_channel_id"],
        updated_at=r["updated_at"],
    )


def get_associated_text_channel(
    db: Database, guild_id: str, voice_channel_id: str | None
) -> str | None:
    """Return the text channel nested under ``voice_channel_id``, if discovered.

    Discord's "text chat in voice channels" creates a ``GUILD_TEXT`` channel
    whose ``parent_id`` is the voice channel's id. Defaulting a server's
    *Now Playing* posts to it keeps updates in the voice channel's own chat.
    """
    if not voice_channel_id:
        return None
    vcid = str(voice_channel_id)
    for ch in get_guild_channels(db, guild_id):
        if ch.channel_type == "text" and (ch.parent_id or None) == vcid:
            return ch.channel_id
    return None


def get_guild_configs(db: Database) -> list[GuildConfig]:
    rows = db.fetchall(
        "SELECT * FROM guild_configs ORDER BY enabled DESC, guild_name ASC, guild_id ASC"
    )
    return [_row(r) for r in rows]


def get_guild_config(db: Database, guild_id: str) -> GuildConfig | None:
    row = db.fetchone("SELECT * FROM guild_configs WHERE guild_id=?", (guild_id,))
    return _row(row) if row is not None else None


def get_enabled_guild_configs(db: Database) -> list[GuildConfig]:
    return [c for c in get_guild_configs(db) if c.enabled]


def get_guild_channels(db: Database, guild_id: str) -> list[ChannelRow]:
    rows = db.fetchall(
        "SELECT * FROM guild_channels WHERE guild_id=? ORDER BY channel_type, channel_name",
        (guild_id,),
    )
    return [
        ChannelRow(
            guild_id=r["guild_id"],
            channel_id=r["channel_id"],
            channel_name=r["channel_name"],
            channel_type=r["channel_type"],
            parent_id=r["parent_id"] if r["parent_id"] is not None else None,
        )
        for r in rows
    ]
