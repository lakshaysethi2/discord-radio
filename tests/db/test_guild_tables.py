"""Schema-level checks for the multi-server tables + the guild_id backfill."""

from __future__ import annotations

from db import guilds as guilds_db


def test_guild_tables_created(db) -> None:
    names = {r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "guild_configs" in names
    assert "guild_channels" in names


def test_watch_sessions_has_guild_id(db) -> None:
    cols = {r["name"] for r in db.fetchall("PRAGMA table_info(watch_sessions)")}
    assert "guild_id" in cols


def test_migration_idempotent_with_new_tables(db) -> None:
    # Re-running migrate must not raise even after the new tables/columns exist.
    db.migrate()
    db.migrate()
    test_guild_tables_created(db)
    test_watch_sessions_has_guild_id(db)


def test_guild_crud_roundtrip(db) -> None:
    guilds_db.discover_guild(db, "1", "X")
    assert guilds_db.get_guild_config(db, "1").guild_name == "X"
