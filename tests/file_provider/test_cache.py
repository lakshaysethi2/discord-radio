from __future__ import annotations

from pathlib import Path

from file_provider.cache import Cache
from file_provider.db import ProviderDB


def _mk_file(cache: Cache, track_id: str, size: int) -> Path:
    p = cache.path_for(track_id, ".mp3")
    p.write_bytes(b"x" * size)
    cache.record(track_id, p)
    return p


def test_record_and_get(cache: Cache, db: ProviderDB) -> None:
    p = _mk_file(cache, "t1", 100)
    got = cache.get("t1")
    assert got == p
    assert cache.total_bytes() == 100


def test_get_missing_returns_none(cache: Cache) -> None:
    assert cache.get("nope") is None


def test_get_drops_row_if_file_vanished(cache: Cache) -> None:
    p = _mk_file(cache, "t1", 50)
    p.unlink()
    assert cache.get("t1") is None
    assert cache.total_bytes() == 0


def test_evict_until_free_lru(cache: Cache) -> None:
    # max_bytes = 10_240 from fixture. Add three 4 KB files.
    _mk_file(cache, "old", 4 * 1024)
    # Nudge timestamps by touching newer ones after a tiny sleep so their
    # last_accessed is strictly greater.
    import time

    time.sleep(0.01)
    _mk_file(cache, "mid", 4 * 1024)
    time.sleep(0.01)
    _mk_file(cache, "new", 4 * 1024)

    # 12 KB used, cap 10 KB — should already be over. Need 4 KB for a new file.
    freed = cache.evict_until_free(4 * 1024)
    assert freed > 0
    # `old` must be gone; `new` must be kept.
    assert cache.get("old") is None
    assert cache.get("new") is not None


def test_evict_respects_protect(cache: Cache) -> None:
    _mk_file(cache, "old", 4 * 1024)
    import time

    time.sleep(0.01)
    _mk_file(cache, "new", 4 * 1024)
    _mk_file(cache, "extra", 4 * 1024)

    cache.evict_until_free(4 * 1024, protect={"old"})
    assert cache.get("old") is not None  # protected


def test_evict_noop_when_under_cap(cache: Cache) -> None:
    _mk_file(cache, "small", 100)
    assert cache.evict_until_free(100) == 0


def test_prune_orphans(cache: Cache) -> None:
    p = _mk_file(cache, "t1", 100)
    p.unlink()
    assert cache.prune_orphans() == 1
    assert cache.total_bytes() == 0


def test_rebuild_from_disk(cache: Cache, db: ProviderDB) -> None:
    orphan = cache.root / "orphan.mp3"
    orphan.write_bytes(b"y" * 200)
    added = cache.rebuild_from_disk()
    assert added == 1
    assert db.cache_entry("orphan") is not None


def test_clear_all(cache: Cache) -> None:
    _mk_file(cache, "a", 50)
    _mk_file(cache, "b", 50)
    cache.clear_all()
    assert cache.total_bytes() == 0
    assert not any(cache.root.iterdir())


def test_free_bytes(cache: Cache) -> None:
    _mk_file(cache, "a", 1024)
    assert cache.free_bytes() == cache.max_bytes - 1024
