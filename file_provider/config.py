"""Environment-driven config for the file-provider service.

Kept in one place so tests can override cleanly and there's no scattered
`os.environ.get` in business logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


@dataclass(slots=True)
class Config:
    db_path: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_DB_PATH", "./data/provider.db")
    )
    cache_path: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_CACHE_PATH", "./cache")
    )
    cache_max_gb: int = field(default_factory=lambda: _env_int("FILE_PROVIDER_CACHE_MAX_GB", 10))

    host: str = field(default_factory=lambda: os.environ.get("FILE_PROVIDER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("FILE_PROVIDER_PORT", 8001))

    provider_order: list[str] = field(
        default_factory=lambda: _env_list("FILE_PROVIDER_ORDER", "local")
    )

    # ---- Local ----
    local_media_path: str = field(
        default_factory=lambda: os.environ.get("LOCAL_MEDIA_PATH", "./media")
    )

    # ---- archive.org ----
    # Comma-separated Internet Archive item ids (e.g.
    # "Hawkins_Lectures_transcoded_actual_files"). Each item's original-source
    # audio files become playlist entries. Public API, no auth needed.
    archive_org_items: list[str] = field(default_factory=lambda: _env_list("ARCHIVE_ORG_ITEMS", ""))

    # ---- Telegram ----
    telegram_api_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_API_ID", ""))
    telegram_api_hash: str = field(default_factory=lambda: os.environ.get("TELEGRAM_API_HASH", ""))
    telegram_channel_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHANNEL_ID", "")
    )

    @property
    def cache_max_bytes(self) -> int:
        return self.cache_max_gb * 1024**3

    def cache_dir(self) -> Path:
        p = Path(self.cache_path)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def telethon_session_path(self) -> Path:
        # Kept alongside the provider DB so it's on the same mounted volume.
        return Path(self.db_path).parent / "telethon.session.txt"


def load() -> Config:
    """Build a fresh Config from the current environment."""
    return Config()
