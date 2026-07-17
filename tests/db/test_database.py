"""Migration + primitives + bot_state tests for db.database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.database import Database, connect
from db.models import SCHEMA


# ------------------------------------------------------------------- fixtures
@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "t.db")
    yield db
    db.close()


# --------------------------------------------------------------------- tests
class TestMigrations:
    def test_creates_all_tables(self, tmp_db: Database) -> None:
        rows = tmp_db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r["name"] for r in rows}
        expected = {
            "tracks",
            "watch_sessions",
            "user_totals",
            "bot_state",
            "monthly_snapshots",
            "dashboard_commands",
        }
        assert expected.issubset(names), f"missing tables: {expected - names}"

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "t.db")
        try:
            db.migrate()
            db.migrate()  # second call must not raise
            # sanity: we can still write and read
            db.set_state("k", "v")
            assert db.get_state("k") == "v"
        finally:
            db.close()

    def test_schema_ddl_stable_order(self) -> None:
        # SCHEMA must be a tuple so the ordering is not accidentally shuffled.
        assert isinstance(SCHEMA, tuple)
        assert len(SCHEMA) > 5


class TestPragmas:
    def test_wal_mode(self, tmp_db: Database) -> None:
        row = tmp_db.fetchone("PRAGMA journal_mode")
        assert row[0].lower() == "wal"

    def test_foreign_keys_on(self, tmp_db: Database) -> None:
        row = tmp_db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1


class TestBotState:
    def test_set_then_get(self, tmp_db: Database) -> None:
        tmp_db.set_state("current_track_id", "abc")
        assert tmp_db.get_state("current_track_id") == "abc"

    def test_set_overwrites(self, tmp_db: Database) -> None:
        tmp_db.set_state("x", "1")
        tmp_db.set_state("x", "2")
        assert tmp_db.get_state("x") == "2"

    def test_get_default(self, tmp_db: Database) -> None:
        assert tmp_db.get_state("missing", "fallback") == "fallback"

    def test_int_helpers(self, tmp_db: Database) -> None:
        tmp_db.set_state("n", 42)
        assert tmp_db.get_state_int("n") == 42
        assert tmp_db.get_state_int("missing", 7) == 7
        tmp_db.set_state("n", "not-a-number")
        assert tmp_db.get_state_int("n", 3) == 3

    def test_bool_helpers(self, tmp_db: Database) -> None:
        tmp_db.set_state("b", True)
        assert tmp_db.get_state_bool("b") is True
        tmp_db.set_state("b", "false")
        assert tmp_db.get_state_bool("b") is False
        assert tmp_db.get_state_bool("missing", True) is True

    def test_none_stored_as_empty(self, tmp_db: Database) -> None:
        tmp_db.set_state("k", None)
        assert tmp_db.get_state("k") == ""
        assert tmp_db.get_state_bool("k", True) is True  # empty falls back


class TestTransaction:
    def test_commits_on_success(self, tmp_db: Database) -> None:
        with tmp_db.transaction() as cur:
            cur.execute(
                "INSERT INTO tracks(track_id, title, playlist_position) VALUES(?,?,?)",
                ("t1", "one", 0),
            )
        assert tmp_db.fetchone("SELECT title FROM tracks WHERE track_id='t1'")["title"] == "one"

    def test_rollback_on_exception(self, tmp_db: Database) -> None:
        with pytest.raises(RuntimeError):
            with tmp_db.transaction() as cur:
                cur.execute(
                    "INSERT INTO tracks(track_id, title, playlist_position) VALUES(?,?,?)",
                    ("t1", "one", 0),
                )
                raise RuntimeError("boom")
        assert tmp_db.fetchone("SELECT 1 FROM tracks WHERE track_id='t1'") is None


class TestConnectShim:
    def test_connect_returns_database(self, tmp_path: Path) -> None:
        db = connect(tmp_path / "s.db")
        try:
            assert isinstance(db, Database)
        finally:
            db.close()

    def test_default_path_uses_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "env.db"
        monkeypatch.setenv("DATABASE_PATH", str(target))
        db = connect()
        try:
            assert Path(db.path) == target
            assert target.exists()
        finally:
            db.close()


class TestUniqueConstraint:
    """The blueprint calls out `playlist_position INTEGER UNIQUE` — verify."""

    def test_duplicate_playlist_position_rejected(self, tmp_db: Database) -> None:
        tmp_db.execute(
            "INSERT INTO tracks(track_id, title, playlist_position) VALUES(?,?,?)",
            ("a", "A", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                "INSERT INTO tracks(track_id, title, playlist_position) VALUES(?,?,?)",
                ("b", "B", 1),
            )
