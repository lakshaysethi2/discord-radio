"""Dashboard server-management routes (§servers)."""

from __future__ import annotations

from db import guilds as guilds_db


class TestServersPage:
    def test_requires_login(self, client) -> None:
        r = client.get("/servers")
        assert r.status_code == 307
        assert r.headers["location"] == "/login"

    def test_lists_discovered_servers(self, client, admin_cookie, db) -> None:
        guilds_db.discover_guild(db, "1", "Server One")
        guilds_db.replace_guild_channels(
            db,
            "1",
            [
                guilds_db.ChannelRow("1", "v1", "Lounge", "voice"),
                guilds_db.ChannelRow("1", "t1", "general", "text"),
            ],
        )
        r = client.get("/servers", cookies=admin_cookie)
        assert r.status_code == 200
        assert "Server One" in r.text
        assert "Lounge" in r.text
        assert "general" in r.text


class TestServersUpdate:
    def _seed(self, db) -> None:
        guilds_db.discover_guild(db, "1", "Server One")
        guilds_db.replace_guild_channels(
            db,
            "1",
            [
                guilds_db.ChannelRow("1", "v1", "Lounge", "voice"),
                guilds_db.ChannelRow("1", "t1", "general", "text"),
            ],
        )

    def test_saves_enabled_and_channels(self, client, admin_cookie, db) -> None:
        self._seed(db)
        r = client.post(
            "/servers/update",
            data={
                "guild_id": "1",
                "enabled": "on",
                "voice_channel_id": "v1",
                "text_channel_id": "t1",
                "csrf": "csrf-test",
            },
            cookies=admin_cookie,
        )
        assert r.status_code == 303
        cfg = guilds_db.get_guild_config(db, "1")
        assert cfg.enabled is True
        assert cfg.voice_channel_id == "v1"
        assert cfg.text_channel_id == "t1"

    def test_rejects_bad_csrf(self, client, admin_cookie, db) -> None:
        self._seed(db)
        r = client.post(
            "/servers/update",
            data={
                "guild_id": "1",
                "enabled": "on",
                "voice_channel_id": "v1",
                "text_channel_id": "t1",
                "csrf": "wrong",
            },
            cookies=admin_cookie,
        )
        assert r.status_code == 403

    def test_rejects_unknown_guild(self, client, admin_cookie, db) -> None:
        r = client.post(
            "/servers/update",
            data={
                "guild_id": "999",
                "enabled": "on",
                "voice_channel_id": "v1",
                "text_channel_id": "t1",
                "csrf": "csrf-test",
            },
            cookies=admin_cookie,
        )
        assert r.status_code == 400

    def test_ignores_channel_ids_not_in_guild(self, client, admin_cookie, db) -> None:
        self._seed(db)
        # Try to point the bot at channels that don't belong to this guild.
        r = client.post(
            "/servers/update",
            data={
                "guild_id": "1",
                "enabled": "on",
                "voice_channel_id": "evil",
                "text_channel_id": "evil2",
                "csrf": "csrf-test",
            },
            cookies=admin_cookie,
        )
        assert r.status_code == 303
        cfg = guilds_db.get_guild_config(db, "1")
        # Untrusted channel ids must be dropped, not stored.
        assert cfg.voice_channel_id is None
        assert cfg.text_channel_id is None
