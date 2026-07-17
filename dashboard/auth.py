"""Discord OAuth2 login for the dashboard.

Flow:
    /login  → 302 to https://discord.com/oauth2/authorize?...
    Discord → /callback?code=...&state=...
    /callback → exchange code for token → fetch user → check admin whitelist
                → set signed session cookie with `user_id`, `username`.

We use `itsdangerous` for the cookie signature (installed as a fastapi dep).
Kept small on purpose — no authlib dependency, so we can test the flow with
a mocked httpx client.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

log = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"
OAUTH_SCOPES = "identify"

SESSION_COOKIE_NAME = "tvbot_session"
OAUTH_STATE_COOKIE = "tvbot_oauth_state"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 1 week


class OAuthError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class SessionUser:
    user_id: str
    username: str

    @property
    def is_admin(self) -> bool:
        return True  # only admins can ever have a session (checked at login)


def build_login_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Discord authorize URL (used by /login)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "none",
    }
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"


def generate_state() -> str:
    return secrets.token_urlsafe(24)


class SessionSigner:
    """Signed-cookie codec for session data. Wraps itsdangerous."""

    def __init__(self, secret_key: str) -> None:
        self._s = URLSafeSerializer(secret_key, salt="tvbot-session")

    def encode(self, data: dict[str, Any]) -> str:
        return self._s.dumps(data)

    def decode(self, token: str) -> dict[str, Any] | None:
        try:
            data = self._s.loads(token)
        except BadSignature:
            return None
        if not isinstance(data, dict):
            return None
        return data


async def exchange_code_for_token(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """POST to Discord's token endpoint. Returns the parsed JSON."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    r = await http.post(
        DISCORD_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        raise OAuthError(f"token exchange failed: {r.status_code} {r.text[:200]}")
    return r.json()


async def fetch_discord_user(*, access_token: str, http: httpx.AsyncClient) -> dict[str, Any]:
    r = await http.get(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        raise OAuthError(f"users/@me failed: {r.status_code}")
    return r.json()


def is_admin(user_id: str, admin_ids: frozenset[str]) -> bool:
    return user_id in admin_ids
