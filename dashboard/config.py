"""Environment-driven config for the dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _env(key: str, default: str = "", *, required: bool = False) -> str:
    v = os.environ.get(key, default)
    if required and not v:
        raise ConfigError(f"required env var {key!r} is missing")
    return v


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_id_list(key: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.environ.get(key, "").split(",") if x.strip())


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True, frozen=True)
class DashboardConfig:
    port: int
    secret_key: str
    database_path: str
    file_provider_base_url: str
    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    superadmin_password: str = ""
    admin_user_ids: frozenset[str] = field(default_factory=frozenset)
    archive_gui_enabled: bool = False

    @property
    def oauth_configured(self) -> bool:
        return bool(
            self.discord_client_id and self.discord_client_secret and self.discord_redirect_uri
        )


def load() -> DashboardConfig:
    return DashboardConfig(
        port=_env_int("DASHBOARD_PORT", 8000),
        secret_key=_env("DASHBOARD_SECRET_KEY", "insecure-dev-key-change-me"),
        database_path=_env("DATABASE_PATH", "./data/tv.db"),
        file_provider_base_url=_env("FILE_PROVIDER_BASE_URL", "http://file-provider:8001"),
        discord_client_id=_env("DISCORD_CLIENT_ID"),
        discord_client_secret=_env("DISCORD_CLIENT_SECRET"),
        discord_redirect_uri=_env("DISCORD_REDIRECT_URI"),
        superadmin_password=_env("SUPERADMIN_PASSWORD"),
        admin_user_ids=_env_id_list("ADMIN_USER_IDS"),
        archive_gui_enabled=_env_bool("ARCHIVE_GUI_ENABLED", default=False),
    )
