from __future__ import annotations

import pytest

from dashboard import config


class TestLoad:
    def test_all_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHBOARD_PORT", "9000")
        monkeypatch.setenv("DASHBOARD_SECRET_KEY", "secret")
        monkeypatch.setenv("DATABASE_PATH", "/tmp/x.db")
        monkeypatch.setenv("FILE_PROVIDER_BASE_URL", "http://p")
        monkeypatch.setenv("DISCORD_CLIENT_ID", "cid")
        monkeypatch.setenv("DISCORD_CLIENT_SECRET", "csec")
        monkeypatch.setenv("DISCORD_REDIRECT_URI", "http://cb")
        monkeypatch.setenv("ADMIN_USER_IDS", "1,2,3")
        cfg = config.load()
        assert cfg.port == 9000
        assert cfg.secret_key == "secret"
        assert cfg.admin_user_ids == frozenset({"1", "2", "3"})
        assert cfg.oauth_configured is True

    def test_oauth_not_configured_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
        monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("DISCORD_REDIRECT_URI", raising=False)
        cfg = config.load()
        assert cfg.oauth_configured is False

    def test_admin_ids_empty_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
        cfg = config.load()
        assert cfg.admin_user_ids == frozenset()
