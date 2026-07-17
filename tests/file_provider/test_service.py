from __future__ import annotations

import threading
import time

import pytest

from file_provider.providers.base import ProviderFetchError
from file_provider.service import PlaylistEmpty, Service


def _wait_prefetch(service: Service, timeout: float = 2.0) -> None:
    t = service._prefetch_thread
    if t is not None:
        t.join(timeout)


class TestRefresh:
    def test_populates_playlist(self, service: Service) -> None:
        assert service.db.playlist_length() == 3

    def test_refresh_returns_stats(self, db, cache, fake_provider) -> None:
        s = Service(db, cache, [fake_provider])
        stats = s.refresh_playlist()
        assert stats["added"] == 3
        assert stats["updated"] == 0
        assert stats["errors"] == {}
        stats = s.refresh_playlist()
        assert stats["added"] == 0
        assert stats["updated"] == 3


class TestCurrentAndNext:
    def test_current_returns_first(self, service: Service) -> None:
        t = service.current()
        assert t.playlist_position == 0
        assert t.title == "Track s1"
        assert t.ready is True
        _wait_prefetch(service)

    def test_next_advances(self, service: Service) -> None:
        service.current()
        t = service.next()
        assert t.playlist_position == 1
        assert t.title == "Track s2"
        _wait_prefetch(service)

    def test_next_wraps(self, service: Service) -> None:
        service.next()  # 1
        service.next()  # 2
        t = service.next()  # wraps to 0
        assert t.playlist_position == 0
        _wait_prefetch(service)

    def test_current_fetches_file(self, service: Service, fake_provider) -> None:
        t = service.current()
        from pathlib import Path

        assert Path(t.local_path).exists()
        assert "s1" in fake_provider.fetches
        _wait_prefetch(service)

    def test_current_uses_cache(self, service: Service, fake_provider) -> None:
        service.current()
        _wait_prefetch(service)
        before = list(fake_provider.fetches)
        service.current()  # should hit cache
        # allow prefetch calls, but s1 should not be re-fetched
        assert before.count("s1") == fake_provider.fetches.count("s1")

    def test_provider_failure_raises(self, db, cache, fake_provider) -> None:
        fake_provider.fail.add("s1")
        s = Service(db, cache, [fake_provider])
        s.refresh_playlist()
        with pytest.raises(ProviderFetchError):
            s.current()


class TestPeek:
    def test_returns_upcoming(self, service: Service) -> None:
        peek = service.peek(3)
        assert [p.title for p in peek] == ["Track s1", "Track s2", "Track s3"]

    def test_peek_wraps(self, service: Service) -> None:
        service.next()
        service.next()
        _wait_prefetch(service)
        peek = service.peek(3)
        assert [p.title for p in peek] == ["Track s3", "Track s1", "Track s2"]


class TestEmpty:
    def test_current_on_empty_raises(self, db, cache, fake_provider) -> None:
        s = Service(db, cache, [fake_provider])
        # do NOT refresh
        with pytest.raises(PlaylistEmpty):
            s.current()


class TestPrefetch:
    def test_next_track_prefetched(self, service: Service) -> None:
        service.current()
        _wait_prefetch(service)
        # cache should contain s1 and s2 now
        peek = service.peek(2)
        assert peek[0].ready
        assert peek[1].ready


class TestConcurrency:
    def test_concurrent_current_is_safe(self, service: Service) -> None:
        results = []
        errors = []

        def worker():
            try:
                results.append(service.current().title)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert all(r == "Track s1" for r in results)
        _wait_prefetch(service)
        # Sleep so prefetch has a chance to run
        time.sleep(0.05)
