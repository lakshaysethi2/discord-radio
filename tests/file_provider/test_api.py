from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from file_provider.api.main import create_app


@pytest.fixture
def client(service):
    app = create_app(service=service)
    return TestClient(app)


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["playlist_length"] == 3
    assert "providers" in body


def test_current(client) -> None:
    r = client.get("/current")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Track s1"
    assert body["ready"] is True
    assert body["playlist_position"] == 0


def test_next(client) -> None:
    r = client.post("/next")
    assert r.status_code == 200
    assert r.json()["title"] == "Track s2"


def test_peek(client) -> None:
    r = client.get("/peek?count=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["title"] == "Track s1"


def test_peek_bounds_enforced(client) -> None:
    r = client.get("/peek?count=0")
    assert r.status_code == 422  # Query(ge=1)


def test_track_by_id(client, service) -> None:
    peek = service.peek(1)
    tid = peek[0].track_id
    r = client.get(f"/tracks/{tid}")
    assert r.status_code == 200
    assert r.json()["track_id"] == tid


def test_track_by_id_missing(client) -> None:
    r = client.get("/tracks/unknown")
    assert r.status_code == 404


def test_mark_played(client, service) -> None:
    tid = service.peek(1)[0].track_id
    r = client.post(f"/tracks/{tid}/played")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_refresh(client) -> None:
    r = client.post("/refresh")
    assert r.status_code == 200
    assert "total" in r.json()


def test_empty_playlist_returns_404(tmp_path):
    from file_provider.cache import Cache
    from file_provider.db import ProviderDB
    from file_provider.service import Service

    db = ProviderDB(tmp_path / "e.db")
    cache = Cache(tmp_path / "c", db, 1024)
    from tests.file_provider.conftest import FakeProvider

    empty = FakeProvider({})
    s = Service(db, cache, [empty])
    # do not refresh
    app = create_app(service=s)
    client = TestClient(app)
    r = client.get("/current")
    assert r.status_code == 404


def test_tracks_page_and_search(client) -> None:
    response = client.get("/tracks?offset=0&limit=2&q=s2")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["Track s2"]


def test_jump_sets_cursor(client, service) -> None:
    target = client.get("/peek?count=3").json()[2]["track_id"]
    response = client.post(f"/jump/{target}")
    assert response.status_code == 200
    assert response.json()["track_id"] == target
    assert service.db.get_cursor() == 2
