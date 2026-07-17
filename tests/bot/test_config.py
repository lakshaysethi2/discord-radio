from __future__ import annotations

import pytest

from bot import config


class TestLoad:
    def test_required_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("DISCORD_GUILD_ID", "1")
        monkeypatch.setenv("DISCORD_VOICE_CHANNEL_ID", "2")
        monkeypatch.setenv("DISCORD_TEXT_CHANNEL_ID", "3")
        monkeypatch.setenv("ADMIN_USER_IDS", "42, 43 , 44")
        cfg = config.load()
        assert cfg.token == "tok"
        assert cfg.guild_id == 1
        assert cfg.voice_channel_id == 2
        assert cfg.text_channel_id == 3
        assert cfg.admin_user_ids == frozenset({"42", "43", "44"})
        # defaults
        assert cfg.min_session_seconds == 30
        assert cfg.checkpoint_interval_seconds == 3600
        assert cfg.file_provider_base_url == "http://file-provider:8001"

    def test_missing_required_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.setenv("DISCORD_GUILD_ID", "1")
        monkeypatch.setenv("DISCORD_VOICE_CHANNEL_ID", "2")
        monkeypatch.setenv("DISCORD_TEXT_CHANNEL_ID", "3")
        with pytest.raises(config.ConfigError):
            config.load()

    def test_bad_int_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("DISCORD_BOT_TOKEN",):
            monkeypatch.setenv(k, "tok")
        monkeypatch.setenv("DISCORD_GUILD_ID", "not-a-number")
        monkeypatch.setenv("DISCORD_VOICE_CHANNEL_ID", "2")
        monkeypatch.setenv("DISCORD_TEXT_CHANNEL_ID", "3")
        with pytest.raises(ValueError):
            config.load()

    def test_env_int_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "5")
        assert config._env_int("X", 999) == 5
        monkeypatch.setenv("X", "not-a-number")
        assert config._env_int("X", 999) == 999
        monkeypatch.delenv("X", raising=False)
        assert config._env_int("X", 999) == 999
