from __future__ import annotations

import sqlite3

from file_provider.db import STATE_CURSOR, ProviderDB


def test_empty_playlist(db: ProviderDB) -> None:
    assert db.playlist_length() == 0
    assert db.get_cursor() == 0
    assert db.track_at(0) is None
    assert db.peek(0, 10) == []


def _insert(db: ProviderDB, refs: list[str]) -> None:
    rows = [
        {
            "track_id": f"fake_{r}",
            "title": f"T{r}",
            "duration_seconds": 60,
            "size_bytes": 100,
            "provider": "fake",
            "source_ref": r,
            "sort_order": float(i),
        }
        for i, r in enumerate(refs)
    ]
    db.upsert_tracks(rows)


def test_upsert_adds_then_updates(db: ProviderDB) -> None:
    added, updated = db.upsert_tracks(
        [
            {
                "track_id": "fake_a",
                "title": "A",
                "duration_seconds": 10,
                "size_bytes": 0,
                "provider": "fake",
                "source_ref": "a",
                "sort_order": 0.0,
            }
        ]
    )
    assert (added, updated) == (1, 0)

    added, updated = db.upsert_tracks(
        [
            {
                "track_id": "fake_a",
                "title": "A2",
                "duration_seconds": 20,
                "size_bytes": 0,
                "provider": "fake",
                "source_ref": "a",
                "sort_order": 0.0,
            }
        ]
    )
    assert (added, updated) == (0, 1)

    row = db.fetchone("SELECT title, duration_seconds FROM tracks")
    assert row["title"] == "A2"
    assert row["duration_seconds"] == 20


def test_cursor_wraps(db: ProviderDB) -> None:
    _insert(db, ["a", "b", "c"])
    assert db.get_cursor() == 0
    assert db.advance_cursor(1) == 1
    assert db.advance_cursor(1) == 2
    assert db.advance_cursor(1) == 0
    assert db.advance_cursor(5) == 2


def test_track_at_wraps(db: ProviderDB) -> None:
    _insert(db, ["a", "b", "c"])
    assert db.track_at(0)["source_ref"] == "a"
    assert db.track_at(2)["source_ref"] == "c"
    assert db.track_at(3)["source_ref"] == "a"  # wrap
    assert db.track_at(-1) is not None  # negative wraps via Python modulo


def test_peek_wraps(db: ProviderDB) -> None:
    _insert(db, ["a", "b", "c"])
    got = db.peek(2, 4)  # start at c, wrap to a, b (only 3 unique available)
    refs = [r["source_ref"] for r in got]
    # Expect [c, a, b, c] — wrap-around continues from the top.
    assert refs[0] == "c"
    assert len(refs) == 4


def test_state_kv(db: ProviderDB) -> None:
    db.set_state("k", "v")
    assert db.get_state("k") == "v"
    assert db.get_state("missing", "d") == "d"


def test_cursor_key_constant() -> None:
    assert STATE_CURSOR == "playlist_cursor"


def test_health_snapshot(db: ProviderDB) -> None:
    db.mark_provider("local", healthy=True)
    db.mark_provider("telegram", healthy=False, error="nope")
    snap = db.health_snapshot()
    assert snap["local"]["healthy"] is True
    assert snap["telegram"]["healthy"] is False
    assert snap["telegram"]["last_error"] == "nope"


def test_set_cursor_normalizes(db: ProviderDB) -> None:
    _insert(db, ["a", "b", "c"])
    db.set_cursor(7)
    assert db.get_cursor() == 1  # 7 % 3
    db.set_cursor(-1)
    assert db.get_cursor() == 2  # -1 % 3 == 2 in Python


def test_cache_lifecycle(db: ProviderDB, tmp_path) -> None:
    _insert(db, ["a"])
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x" * 100)
    db.record_cache("fake_a", str(p), 100)
    row = db.cache_entry("fake_a")
    assert row["size_bytes"] == 100
    assert db.cache_total_bytes() == 100
    db.forget_cache("fake_a")
    assert db.cache_entry("fake_a") is None


# ------------------------------------------------------------- has_video column
def test_has_video_column_default_false(db: ProviderDB) -> None:
    _insert(db, ["a"])
    row = db.fetchone("SELECT has_video FROM tracks WHERE source_ref='a'")
    assert row["has_video"] == 0


def test_has_video_column_persists(db: ProviderDB) -> None:
    db.upsert_tracks(
        [
            {
                "track_id": "fake_v",
                "title": "video",
                "duration_seconds": 10,
                "size_bytes": 100,
                "provider": "fake",
                "source_ref": "v",
                "sort_order": 0.0,
                "has_video": True,
            }
        ]
    )
    row = db.fetchone("SELECT has_video FROM tracks WHERE source_ref='v'")
    assert row["has_video"] == 1


def test_migration_adds_has_video_to_legacy_db(tmp_path) -> None:
    """A DB created without the column should get it added on open."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Create a legacy schema WITHOUT has_video.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tracks (
            track_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            duration_seconds INTEGER DEFAULT 0,
            size_bytes INTEGER DEFAULT 0,
            provider TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            sort_order REAL NOT NULL,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, source_ref)
        )
        """
    )
    conn.execute(
        "INSERT INTO tracks(track_id, title, provider, source_ref, sort_order) "
        "VALUES('legacy', 'Legacy', 'p', 'x', 0)"
    )
    conn.commit()
    conn.close()

    # Now open with the new schema — migration must add has_video.
    with ProviderDB(db_path) as new_db:
        cols = new_db.fetchall("PRAGMA table_info(tracks)")
        col_names = {row[1] for row in cols}
        assert "has_video" in col_names
        # Existing row survives with the default 0.
        row = new_db.fetchone("SELECT has_video FROM tracks WHERE track_id='legacy'")
        assert row["has_video"] == 0


def test_migration_idempotent(tmp_path) -> None:
    """Calling migrate() twice on an already-migrated DB must not raise."""
    db = ProviderDB(tmp_path / "idem.db")
    try:
        db.migrate()  # explicit second call
        db.migrate()  # third call
    finally:
        db.close()


def test_list_all_search_retains_natural_playlist_position(db: ProviderDB) -> None:
    _insert(db, ["a", "b", "c"])
    row = db.list_all(search="Tc")[0]
    assert row["playlist_position"] == 2


def test_unwritable_directory_raises(tmp_path) -> None:
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        import pytest

        with pytest.raises((PermissionError, sqlite3.OperationalError)) as exc_info:
            ProviderDB(ro_dir / "fail.db")
        assert "not writable" in str(exc_info.value)
    finally:
        ro_dir.chmod(0o755)
