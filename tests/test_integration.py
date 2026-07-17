"""End-to-end integration tests: bot's HTTP client against real file-provider app.

Uses ASGI transport so no real TCP socket is needed. Verifies the wire
contract between the two services without mocking either side.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from file_provider.api.main import create_app
from file_provider.cache import Cache
from file_provider.db import ProviderDB
from file_provider.providers.local import LocalProvider
from file_provider.service import Service
from provider.client import FileProviderClient


@pytest.fixture
def live_service(tmp_path: Path):
    """Build a real file-provider Service backed by a local media dir."""
    media = tmp_path / "media"
    media.mkdir()
    # Three tiny mp3-shaped files. Real audio content isn't needed for the
    # HTTP contract — FFmpeg would need real data, but we don't invoke it.
    for name in ("aaa.mp3", "bbb.mp3", "ccc.mp3"):
        (media / name).write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 512)

    db = ProviderDB(tmp_path / "provider.db")
    cache = Cache(tmp_path / "cache", db, max_bytes=10 * 1024 * 1024)
    svc = Service(db=db, cache=cache, providers=[LocalProvider(media)])
    svc.refresh_playlist()
    yield svc
    # Wait for any prefetch, then close DB.
    t = svc._prefetch_thread
    if t is not None:
        t.join(timeout=5.0)
    db.close()


@pytest.fixture
def bot_client(live_service):
    """A FileProviderClient talking to the real Service via ASGI transport."""
    app = create_app(service=live_service)
    transport = httpx.ASGITransport(app=app)
    inner = httpx.AsyncClient(transport=transport, base_url="http://provider")
    return FileProviderClient("http://provider", client=inner)


class TestContract:
    async def test_current_returns_ready_track(self, bot_client, live_service):
        async with bot_client as fp:
            track = await fp.current()
        assert track.ready is True
        assert track.title == "aaa"
        assert track.playlist_position == 0
        assert track.provider_used == "local"
        # Local path is a real file on disk.
        assert Path(track.local_path).exists()

    async def test_next_advances(self, bot_client):
        async with bot_client as fp:
            first = await fp.current()
            second = await fp.next()
        assert first.track_id != second.track_id
        assert second.playlist_position == 1

    async def test_next_wraps(self, bot_client, live_service):
        async with bot_client as fp:
            await fp.next()  # -> pos 1
            await fp.next()  # -> pos 2
            third = await fp.next()  # -> wraps to 0
        assert third.playlist_position == 0

    async def test_peek(self, bot_client):
        async with bot_client as fp:
            peek = await fp.peek(3)
        assert len(peek) == 3
        assert [p.title for p in peek] == ["aaa", "bbb", "ccc"]

    async def test_get_by_id_forces_fetch(self, bot_client, live_service):
        async with bot_client as fp:
            first = await fp.current()
            # Ensure the file is really cached — request by id again.
            again = await fp.get_by_id(first.track_id)
        assert again.local_path == first.local_path
        assert Path(again.local_path).exists()

    async def test_mark_played_no_error(self, bot_client):
        async with bot_client as fp:
            t = await fp.current()
            await fp.mark_played(t.track_id)

    async def test_health(self, bot_client, live_service):
        async with bot_client as fp:
            h = await fp.health()
        assert h["playlist_length"] == 3
        assert "providers" in h


class TestErrorPaths:
    async def test_unknown_track_id_raises(self, bot_client):
        from provider.client import ProviderError

        async with bot_client as fp:
            with pytest.raises(ProviderError):
                await fp.get_by_id("nope")

    async def test_empty_playlist_returns_404(self, tmp_path):
        # Fresh service with no tracks.
        db = ProviderDB(tmp_path / "empty.db")
        cache = Cache(tmp_path / "c", db, max_bytes=1024)
        media = tmp_path / "media"
        media.mkdir()
        svc = Service(db=db, cache=cache, providers=[LocalProvider(media)])
        try:
            app = create_app(service=svc)
            transport = httpx.ASGITransport(app=app)
            inner = httpx.AsyncClient(transport=transport, base_url="http://p")
            client = FileProviderClient("http://p", client=inner)
            async with client as fp:
                from provider.client import ProviderError

                with pytest.raises(ProviderError):
                    await fp.current()
        finally:
            db.close()
