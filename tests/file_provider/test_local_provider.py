from __future__ import annotations

from pathlib import Path

import pytest

from file_provider.providers.base import ProviderFetchError
from file_provider.providers.local import LocalProvider


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mp3").write_bytes(b"aaa")
    (root / "nested").mkdir()
    (root / "nested" / "b.opus").write_bytes(b"bb")
    (root / "ignored.txt").write_bytes(b"x")  # not audio
    (root / "c.flac").write_bytes(b"c")
    return root


def test_list_tracks_recursive_and_sorted(media_root: Path) -> None:
    p = LocalProvider(media_root)
    tracks = p.list_tracks()
    refs = [t.source_ref for t in tracks]
    assert refs == sorted(refs)
    assert any(r.endswith("a.mp3") for r in refs)
    assert any("b.opus" in r for r in refs)
    assert not any("ignored" in r for r in refs)


def test_is_configured_requires_dir(tmp_path: Path) -> None:
    assert LocalProvider(tmp_path / "nope").is_configured() is False
    assert LocalProvider(tmp_path).is_configured() is True


def test_missing_root_yields_empty(tmp_path: Path) -> None:
    p = LocalProvider(tmp_path / "missing")
    assert p.list_tracks() == []


def test_ensure_cached_hardlinks_or_copies(media_root: Path, tmp_path: Path) -> None:
    p = LocalProvider(media_root)
    target = tmp_path / "cache" / "x.mp3"
    out = p.ensure_cached("a.mp3", target)
    assert out.exists()
    assert out.read_bytes() == b"aaa"


def test_ensure_cached_idempotent(media_root: Path, tmp_path: Path) -> None:
    p = LocalProvider(media_root)
    target = tmp_path / "cache" / "x.mp3"
    p.ensure_cached("a.mp3", target)
    # second call is a no-op
    out = p.ensure_cached("a.mp3", target)
    assert out.exists()


def test_ensure_cached_missing_raises(media_root: Path, tmp_path: Path) -> None:
    p = LocalProvider(media_root)
    with pytest.raises(ProviderFetchError):
        p.ensure_cached("does-not-exist.mp3", tmp_path / "y.mp3")


def test_track_id_is_stable() -> None:
    from file_provider.providers.base import ProviderTrack

    t1 = ProviderTrack(title="x", source_ref="ref")
    t2 = ProviderTrack(title="different title", source_ref="ref")
    assert t1.track_id("p") == t2.track_id("p")  # depends only on provider + ref
