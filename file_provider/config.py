"""Environment-driven config for the file-provider service.

Kept in one place so tests can override cleanly and there's no scattered
`os.environ.get` in business logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from file_provider.media_types import PLAYABLE_EXTS


def _env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_extensions(key: str) -> frozenset[str]:
    raw = os.environ.get(key)
    if raw is None:
        return PLAYABLE_EXTS
    return frozenset(
        value if value.startswith(".") else f".{value}"
        for value in (part.strip().lower() for part in raw.split(","))
        if value
    )


@dataclass(slots=True)
class Config:
    db_path: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_DB_PATH", "./data/provider.db")
    )
    cache_path: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_CACHE_PATH", "./cache")
    )
    # Global provider-managed disk quota: playback cache + torrent payloads
    # + provider metadata/session files.
    cache_max_gb: int = field(default_factory=lambda: _env_int("FILE_PROVIDER_CACHE_MAX_GB", 10))

    host: str = field(default_factory=lambda: os.environ.get("FILE_PROVIDER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("FILE_PROVIDER_PORT", 8001))

    provider_order: list[str] = field(
        default_factory=lambda: _env_list("FILE_PROVIDER_ORDER", "local,torrent")
    )

    # ---- Torrent / aria2 -----------------------------------------------
    # The provider starts a local aria2 JSON-RPC daemon in the file-provider
    # container. Keeping the RPC endpoint loopback-only means the dashboard
    # can manage torrents through this service without exposing aria2 itself.
    torrent_enabled: bool = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_TORRENT_ENABLED", "1").lower()
        in {"1", "true", "yes", "on"}
    )
    torrent_rpc_url: str = field(
        default_factory=lambda: os.environ.get(
            "FILE_PROVIDER_TORRENT_RPC_URL", "http://127.0.0.1:6800/jsonrpc"
        )
    )
    torrent_rpc_secret: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_TORRENT_RPC_SECRET", "")
    )
    torrent_rpc_port: int = field(
        default_factory=lambda: _env_int("FILE_PROVIDER_TORRENT_RPC_PORT", 6800)
    )
    torrent_data_path: str = field(
        default_factory=lambda: os.environ.get(
            "FILE_PROVIDER_TORRENT_DATA_PATH", "./data/torrents"
        )
    )
    torrent_binary: str = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_TORRENT_BINARY", "aria2c")
    )
    torrent_allow_remote_rpc: bool = field(
        default_factory=lambda: os.environ.get("FILE_PROVIDER_TORRENT_ALLOW_REMOTE_RPC", "0").lower()
        in {"1", "true", "yes", "on"}
    )
    torrent_max_size_gb: float = field(
        default_factory=lambda: _env_float("FILE_PROVIDER_TORRENT_MAX_SIZE_GB", 10.0)
    )
    torrent_max_upload_mb: int = field(
        default_factory=lambda: _env_int("FILE_PROVIDER_TORRENT_MAX_UPLOAD_MB", 16)
    )
    torrent_allowed_extensions: frozenset[str] = field(
        default_factory=lambda: _env_extensions("FILE_PROVIDER_TORRENT_ALLOWED_EXTENSIONS")
    )

    @property
    def torrent_max_size_bytes(self) -> int:
        # Zero disables the aggregate-size guard for operators who explicitly
        # accept unrestricted downloads.
        return max(0, int(self.torrent_max_size_gb * 1024**3))

    @property
    def torrent_max_upload_bytes(self) -> int:
        return max(1, self.torrent_max_upload_mb) * 1024**2

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
