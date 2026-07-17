"""Tests for the per-guild configuration + channel discovery layer (§servers)."""

from __future__ import annotations

from db import guilds as guilds_db


def test_discover_inserts_and_refreshes_name(db) -> None:
    guilds_db.discover_guild(db, "1", "Server A")
    cfg = guilds_db.get_guild_config(db, "1")
    assert cfg is not None
    assert cfg.guild_name == "Server A"
    assert cfg.enabled is False
    # Rediscover only refreshes the display name, never the admin's choices.
    guilds_db.discover_guild(db, "1", "Server A Renamed")
    cfg = guilds_db.get_guild_config(db, "1")
    assert cfg.guild_name == "Server A Renamed"
    assert cfg.enabled is False


def test_apply_and_get(db) -> None:
    guilds_db.discover_guild(db, "7", "G")
    guilds_db.apply_guild_config(db, "7", enabled=True, voice_channel_id="v1", text_channel_id="t1")
    cfg = guilds_db.get_guild_config(db, "7")
    assert cfg.enabled is True
    assert cfg.voice_channel_id == "v1"
    assert cfg.text_channel_id == "t1"
    assert cfg.updated_at is not None


def test_replace_guild_channels(db) -> None:
    guilds_db.replace_guild_channels(
        db,
        "1",
        [
            guilds_db.ChannelRow("1", "v1", "Voice", "voice"),
            guilds_db.ChannelRow("1", "t1", "General", "text"),
        ],
    )
    ch = guilds_db.get_guild_channels(db, "1")
    assert {c.channel_id for c in ch} == {"v1", "t1"}
    # Replacing must drop the old channels, not append.
    guilds_db.replace_guild_channels(db, "1", [guilds_db.ChannelRow("1", "v2", "Voice2", "voice")])
    ch = guilds_db.get_guild_channels(db, "1")
    assert [c.channel_id for c in ch] == ["v2"]


def test_get_guild_configs_orders_enabled_first(db) -> None:
    guilds_db.discover_guild(db, "a", "A")
    guilds_db.discover_guild(db, "b", "B")
    guilds_db.apply_guild_config(db, "b", enabled=True, voice_channel_id="v", text_channel_id="t")
    ids = [c.guild_id for c in guilds_db.get_guild_configs(db)]
    assert ids == ["b", "a"]


def test_seed_env_guild_enables_and_validates_channels(db) -> None:
    guilds_db.discover_guild(db, "5", "EnvGuild")
    guilds_db.replace_guild_channels(
        db,
        "5",
        [
            guilds_db.ChannelRow("5", "100", "VC", "voice"),
            guilds_db.ChannelRow("5", "200", "TC", "text"),
        ],
    )

    class _Cfg:
        guild_id = 5
        voice_channel_id = 100
        text_channel_id = 200

    guilds_db.seed_env_guild(db, _Cfg())
    cfg = guilds_db.get_guild_config(db, "5")
    assert cfg.enabled is True
    assert cfg.voice_channel_id == "100"
    assert cfg.text_channel_id == "200"


def test_seed_env_guild_skips_when_admin_managed(db) -> None:
    guilds_db.discover_guild(db, "5", "EnvGuild")
    guilds_db.apply_guild_config(db, "5", enabled=True, voice_channel_id="x", text_channel_id="y")

    class _Cfg:
        guild_id = 5
        voice_channel_id = 100
        text_channel_id = 200

    guilds_db.seed_env_guild(db, _Cfg())
    cfg = guilds_db.get_guild_config(db, "5")
    # Admin's choice must not be clobbered by the env bootstrap.
    assert cfg.voice_channel_id == "x"


def test_seed_env_guild_no_channels_does_not_enable(db) -> None:
    guilds_db.discover_guild(db, "5", "EnvGuild")
    # No channels discovered yet — don't point the bot at phantom channels.

    class _Cfg:
        guild_id = 5
        voice_channel_id = 100
        text_channel_id = 200

    guilds_db.seed_env_guild(db, _Cfg())
    cfg = guilds_db.get_guild_config(db, "5")
    assert cfg.enabled is False
