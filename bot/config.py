"""Environment-driven config for the bot.

Kept small on purpose — one dataclass, one loader, no magic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when a required env var is missing / malformed."""


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise ConfigError(f"required env var {key!r} is missing")
    return val or ""


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_id_list(key: str) -> frozenset[str]:
    raw = os.environ.get(key, "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _env_int_or(key: str, default: int) -> int:
    """Like `_env_int` but treats an *unset* var as `default` without raising.

    A present-but-malformed value still raises ``ValueError`` so misconfig is
    surfaced loudly. Used for the optional single-guild bootstrap vars.
    """
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(slots=True, frozen=True)
class BotConfig:
    token: str
    # The three guild-id vars are *optional* legacy bootstrap values (no longer
    # required): if set, they seed the bot's first server on first boot, but the
    # dashboard is the source of truth for which servers the bot speaks in
    # (see db.guilds / §servers).
    guild_id: int = 0
    voice_channel_id: int = 0
    text_channel_id: int = 0
    file_provider_base_url: str = "http://file-provider:8001"
    database_path: str = "./data/tv.db"
    min_session_seconds: int = 30
    checkpoint_interval_seconds: int = 3600
    admin_user_ids: frozenset[str] = field(default_factory=frozenset)


def load() -> BotConfig:
    return BotConfig(
        token=_env("DISCORD_BOT_TOKEN", required=True),
        guild_id=_env_int_or("DISCORD_GUILD_ID", 0),
        voice_channel_id=_env_int_or("DISCORD_VOICE_CHANNEL_ID", 0),
        text_channel_id=_env_int_or("DISCORD_TEXT_CHANNEL_ID", 0),
        file_provider_base_url=_env("FILE_PROVIDER_BASE_URL", "http://file-provider:8001"),
        database_path=_env("DATABASE_PATH", "./data/tv.db"),
        min_session_seconds=_env_int("MIN_SESSION_SECONDS", 30),
        checkpoint_interval_seconds=_env_int("CHECKPOINT_INTERVAL_SECONDS", 3600),
        admin_user_ids=_env_id_list("ADMIN_USER_IDS"),
    )
