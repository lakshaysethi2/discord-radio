from __future__ import annotations

import pytest

from dashboard import auth


class TestSessionSigner:
    def test_roundtrip(self) -> None:
        s = auth.SessionSigner("k" * 32)
        token = s.encode({"user_id": "1", "username": "x"})
        assert s.decode(token) == {"user_id": "1", "username": "x"}

    def test_tampered_returns_none(self) -> None:
        s = auth.SessionSigner("k" * 32)
        token = s.encode({"user_id": "1"})
        # Flip a byte in the middle.
        bad = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
        assert s.decode(bad) is None

    def test_different_secret_rejects(self) -> None:
        a = auth.SessionSigner("secret-a" * 4)
        b = auth.SessionSigner("secret-b" * 4)
        token = a.encode({"u": 1})
        assert b.decode(token) is None

    def test_bogus_token_returns_none(self) -> None:
        s = auth.SessionSigner("k" * 32)
        assert s.decode("this-is-not-a-token") is None


class TestLoginUrl:
    def test_contains_all_params(self) -> None:
        url = auth.build_login_url("cid", "http://x/cb", "st4t3")
        assert url.startswith("https://discord.com/oauth2/authorize?")
        for needle in ["client_id=cid", "state=st4t3", "response_type=code", "scope=identify"]:
            assert needle in url


class TestAdmin:
    def test_in_whitelist(self) -> None:
        assert auth.is_admin("42", frozenset({"42", "43"})) is True

    def test_not_in_whitelist(self) -> None:
        assert auth.is_admin("42", frozenset({"41"})) is False

    def test_empty_whitelist_denies_all(self) -> None:
        assert auth.is_admin("42", frozenset()) is False


class TestGenerateState:
    def test_returns_url_safe_string(self) -> None:
        s = auth.generate_state()
        assert len(s) >= 24
        # url-safe base64 alphabet only
        assert all(c.isalnum() or c in "-_" for c in s)


class TestExchange:
    async def test_success(self) -> None:
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/oauth2/token")
            assert b"code=abc" in request.content
            return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            data = await auth.exchange_code_for_token(
                code="abc",
                client_id="cid",
                client_secret="sec",
                redirect_uri="http://x/cb",
                http=http,
            )
        assert data["access_token"] == "tok"

    async def test_failure_raises(self) -> None:
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad code")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(auth.OAuthError):
                await auth.exchange_code_for_token(
                    code="abc",
                    client_id="cid",
                    client_secret="sec",
                    redirect_uri="http://x/cb",
                    http=http,
                )


class TestFetchUser:
    async def test_success(self) -> None:
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, json={"id": "42", "username": "u", "global_name": "GN"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            data = await auth.fetch_discord_user(access_token="tok", http=http)
        assert data["id"] == "42"
