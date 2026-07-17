"""Shared fixtures for file_provider tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from file_provider.cache import Cache
from file_provider.db import ProviderDB
from file_provider.providers.base import BaseProvider, ProviderTrack
from file_provider.service import Service


@pytest.fixture
def db(tmp_path: Path) -> ProviderDB:
    d = ProviderDB(tmp_path / "provider.db")
    yield d
    d.close()


@pytest.fixture
def cache(tmp_path: Path, db: ProviderDB) -> Cache:
    return Cache(tmp_path / "cache", db, max_bytes=10 * 1024)  # 10 KB for eviction tests


class FakeProvider(BaseProvider):
    """In-memory provider for tests. Files are written from a dict."""

    name = "fake"

    def __init__(self, files: dict[str, bytes]) -> None:
        # files: { source_ref: bytes }
        self.files = files
        self.fetches: list[str] = []
        self.fail: set[str] = set()

    def is_configured(self) -> bool:
        return True

    def list_tracks(self) -> list[ProviderTrack]:
        return [
            ProviderTrack(title=f"Track {ref}", source_ref=ref, size_bytes=len(data))
            for ref, data in self.files.items()
        ]

    def ensure_cached(self, source_ref, target_path):
        self.fetches.append(source_ref)
        if source_ref in self.fail:
            from file_provider.providers.base import ProviderFetchError

            raise ProviderFetchError(f"forced failure for {source_ref}")
        data = self.files.get(source_ref)
        if data is None:
            from file_provider.providers.base import ProviderFetchError

            raise ProviderFetchError(f"unknown {source_ref}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(data)
        return target_path


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider(
        {
            "s1": b"aaaa",  # 4 bytes
            "s2": b"bbbb",
            "s3": b"cccc",
        }
    )


@pytest.fixture
def service(db: ProviderDB, cache: Cache, fake_provider: FakeProvider) -> Service:
    s = Service(db=db, cache=cache, providers=[fake_provider])
    s.refresh_playlist()
    yield s
    # Wait for any in-flight prefetch before the DB gets torn down.
    t = s._prefetch_thread
    if t is not None:
        t.join(timeout=5.0)
