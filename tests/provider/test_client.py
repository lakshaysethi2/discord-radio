"""Tests for the file-provider HTTP client, using respx to mock httpx."""

from __future__ import annotations

import httpx
import pytest
import respx

from provider.client import (
    FileProviderClient,
    ProviderError,
    ProviderUnavailable,
    TrackResponse,
)

BASE = "http://provider:8001"

TRACK_JSON = {
    "track_id": "abc123",
    "title": "The Power of Stillness",
    "duration_seconds": 3600,
    "local_path": "/cache/abc123.mp3",
    "provider_used": "gdrive",
    "playlist_position": 42,
    "ready": True,
}


@pytest.fixture
def client() -> FileProviderClient:
    return FileProviderClient(BASE, timeout=1.0, max_retries=3)


# ---------------------------------------------------------------- TrackResponse
class TestTrackResponse:
    def test_from_json_happy_path(self) -> None:
        t = TrackResponse.from_json(TRACK_JSON)
        assert t.track_id == "abc123"
        assert t.duration_seconds == 3600
        assert t.ready is True
        assert t.playlist_position == 42

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ProviderError):
            TrackResponse.from_json({"title": "no id"})

    def test_defaults_applied(self) -> None:
        # ready defaults True, playlist_position defaults 0 if absent
        t = TrackResponse.from_json(
            {
                "track_id": "a",
                "title": "b",
                "local_path": "/c",
                # missing duration_seconds, provider_used, playlist_position, ready
            }
        )
        assert t.duration_seconds == 0
        assert t.provider_used == "unknown"
        assert t.playlist_position == 0
        assert t.ready is True

    def test_bad_types_rejected(self) -> None:
        with pytest.raises(ProviderError):
            TrackResponse.from_json(
                {"track_id": "a", "title": "b", "local_path": "/c", "duration_seconds": "not-int"}
            )


# ---------------------------------------------------------------------- routes
@respx.mock
async def test_current_returns_track(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/current").mock(return_value=httpx.Response(200, json=TRACK_JSON))
    async with client as fp:
        t = await fp.current()
    assert t.track_id == "abc123"


@respx.mock
async def test_next_uses_post(client: FileProviderClient) -> None:
    route = respx.post(f"{BASE}/next").mock(return_value=httpx.Response(200, json=TRACK_JSON))
    async with client as fp:
        await fp.next()
    assert route.called


@respx.mock
async def test_peek_returns_list(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/peek").mock(
        return_value=httpx.Response(200, json=[TRACK_JSON, {**TRACK_JSON, "track_id": "b"}])
    )
    async with client as fp:
        items = await fp.peek(2)
    assert len(items) == 2
    assert items[1].track_id == "b"


@respx.mock
async def test_peek_zero_short_circuits(client: FileProviderClient) -> None:
    async with client as fp:
        assert await fp.peek(0) == []


@respx.mock
async def test_health_returns_dict(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"local": "ok"}))
    async with client as fp:
        h = await fp.health()
    assert h == {"local": "ok"}


@respx.mock
async def test_get_by_id(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/tracks/abc123").mock(return_value=httpx.Response(200, json=TRACK_JSON))
    async with client as fp:
        t = await fp.get_by_id("abc123")
    assert t.track_id == "abc123"


@respx.mock
async def test_get_by_id_404_raises(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/tracks/nope").mock(return_value=httpx.Response(404))
    async with client as fp:
        with pytest.raises(ProviderError):
            await fp.get_by_id("nope")


@respx.mock
async def test_mark_played_swallows_404(client: FileProviderClient) -> None:
    respx.post(f"{BASE}/tracks/abc/played").mock(return_value=httpx.Response(404))
    async with client as fp:
        await fp.mark_played("abc")  # must not raise


# ----------------------------------------------------------------- retry/error
@respx.mock
async def test_retries_transient_5xx_then_succeeds(client: FileProviderClient) -> None:
    calls = [
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(200, json=TRACK_JSON),
    ]
    route = respx.get(f"{BASE}/current").mock(side_effect=calls)
    async with client as fp:
        t = await fp.current()
    assert route.call_count == 3
    assert t.track_id == "abc123"


@respx.mock
async def test_all_retries_exhausted_raises_unavailable(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/current").mock(return_value=httpx.Response(503))
    async with client as fp:
        with pytest.raises(ProviderUnavailable):
            await fp.current()


@respx.mock
async def test_network_error_retried(client: FileProviderClient) -> None:
    calls = [httpx.ConnectError("boom"), httpx.Response(200, json=TRACK_JSON)]
    route = respx.get(f"{BASE}/current").mock(side_effect=calls)
    async with client as fp:
        t = await fp.current()
    assert route.call_count == 2
    assert t.track_id == "abc123"


@respx.mock
async def test_4xx_not_retried(client: FileProviderClient) -> None:
    route = respx.get(f"{BASE}/current").mock(return_value=httpx.Response(404, text="nope"))
    async with client as fp:
        with pytest.raises(ProviderError):
            await fp.current()
    assert route.call_count == 1


@respx.mock
async def test_non_json_response_raises(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/current").mock(return_value=httpx.Response(200, text="not json"))
    async with client as fp:
        with pytest.raises(ProviderError):
            await fp.current()


# ---------------------------------------------------------------- base_url edge
def test_base_url_strips_trailing_slash() -> None:
    c = FileProviderClient("http://x:1/")
    assert c.base_url == "http://x:1"


def test_max_retries_min_one() -> None:
    c = FileProviderClient(BASE, max_retries=0)
    assert c.max_retries == 1


@respx.mock
async def test_list_tracks_and_jump_to(client: FileProviderClient) -> None:
    respx.get(f"{BASE}/tracks").mock(
        return_value=httpx.Response(200, json={"items": [TRACK_JSON], "total": 1})
    )
    respx.post(f"{BASE}/jump/abc123").mock(return_value=httpx.Response(200, json=TRACK_JSON))
    async with client as fp:
        items, total = await fp.list_tracks(search="stillness")
        jumped = await fp.jump_to("abc123")
    assert total == 1
    assert items[0].track_id == "abc123"
    assert jumped.track_id == "abc123"
