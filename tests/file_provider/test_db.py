from __future__ import annotations

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
