from __future__ import annotations

from pathlib import Path

from file_provider.storage import StorageQuota


def test_storage_quota_counts_hardlinks_once(tmp_path: Path) -> None:
    torrent = tmp_path / "torrents"
    cache = tmp_path / "cache"
    torrent.mkdir()
    cache.mkdir()
    source = torrent / "song.mp3"
    source.write_bytes(b"1234")
    (cache / "track.audio").hardlink_to(source)

    quota = StorageQuota(5, [torrent, cache])
    assert quota.usage_bytes() == 4
    assert quota.allows(1) is True
    assert quota.allows(2) is False


def test_storage_quota_includes_multiple_roots(tmp_path: Path) -> None:
    torrent = tmp_path / "torrents"
    cache = tmp_path / "cache"
    torrent.mkdir()
    cache.mkdir()
    (torrent / "a").write_bytes(b"123")
    (cache / "b").write_bytes(b"4567")

    quota = StorageQuota(10, [torrent, cache])
    assert quota.usage_bytes() == 7
    assert quota.free_bytes() == 3
