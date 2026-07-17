"""Shared fixtures for dashboard tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard.auth import SessionSigner
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from db.database import Database

ADMIN_IDS = frozenset({"111", "222"})


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "dash.db")
    yield d
    d.close()


@pytest.fixture
def config() -> DashboardConfig:
    return DashboardConfig(
        port=8000,
        secret_key="test-secret-key-please-do-not-use-in-prod",
        database_path=":memory:",
        file_provider_base_url="http://provider:8001",
        discord_client_id="test-client-id",
        discord_client_secret="test-client-secret",
        discord_redirect_uri="http://localhost:8000/callback",
        admin_user_ids=ADMIN_IDS,
    )


@pytest.fixture
def http_transport() -> httpx.MockTransport:
    """A no-op mock transport; individual tests override .handler."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="unexpected call")

    return httpx.MockTransport(handler)


@pytest.fixture
def http_client(http_transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=http_transport)


@pytest.fixture
def app(config, db, http_client):
    return create_app(config, db=db, http_client=http_client)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def signer(config: DashboardConfig) -> SessionSigner:
    return SessionSigner(config.secret_key)


@pytest.fixture
def admin_cookie(signer: SessionSigner) -> dict[str, str]:
    """A pre-baked signed session cookie for an admin user."""
    token = signer.encode({"user_id": "111", "username": "AdminUser", "csrf": "csrf-test"})
    return {"tvbot_session": token}


@pytest.fixture
def non_admin_cookie(signer: SessionSigner) -> dict[str, str]:
    token = signer.encode({"user_id": "999", "username": "Rando", "csrf": "csrf-test"})
    return {"tvbot_session": token}
