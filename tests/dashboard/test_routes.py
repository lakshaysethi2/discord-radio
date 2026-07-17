"""End-to-end route tests using FastAPI's TestClient."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from dashboard.main import create_app
from db.database import Database


# --------------------------------------------------------------------- basics
class TestIndex:
    def test_redirect(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 307
        assert r.headers["location"] == "/dashboard"


# ------------------------------------------------------------------------- auth
class TestUnauth:
    def test_dashboard_requires_login(self, client: TestClient) -> None:
        r = client.get("/dashboard")
        assert r.status_code == 307
        assert r.headers["location"] == "/login"

    def test_leaderboard_requires_login(self, client: TestClient) -> None:
        r = client.get("/leaderboard")
        assert r.status_code == 307

    def test_queue_requires_login(self, client: TestClient) -> None:
        r = client.get("/queue")
        assert r.status_code == 307

    def test_controls_requires_login(self, client: TestClient) -> None:
        r = client.post("/controls", data={"action": "skip", "csrf": "x"})
        assert r.status_code == 307


class TestLoginPage:
    def test_login_page_renders_with_both_methods(self, db, http_client) -> None:
        from dashboard.config import DashboardConfig

        cfg = DashboardConfig(
            port=8000,
            secret_key="k" * 32,
            database_path=":memory:",
            file_provider_base_url="",
            discord_client_id="cid",
            discord_client_secret="sec",
            discord_redirect_uri="http://x/cb",
            superadmin_password="super-secret",
            admin_user_ids=frozenset(),
        )
        app = create_app(cfg, db=db, http_client=http_client)
        c = TestClient(app, follow_redirects=False)
        r = c.get("/login")
        assert r.status_code == 200
        assert "Option 1: Discord Sign-in" in r.text
        assert "Superadmin Sign-in" in r.text

    def test_login_discord_redirects_when_configured(self, client: TestClient) -> None:
        r = client.get("/login/discord")
        assert r.status_code == 307
        assert r.headers["location"].startswith("https://discord.com/oauth2/authorize?")
        assert "tvbot_oauth_state" in r.cookies

    def test_login_discord_not_configured_redirects_back(self, db, http_client) -> None:
        from dashboard.config import DashboardConfig

        cfg = DashboardConfig(
            port=8000,
            secret_key="k" * 32,
            database_path=":memory:",
            file_provider_base_url="",
            discord_client_id="",
            discord_client_secret="",
            discord_redirect_uri="",
            superadmin_password="",
            admin_user_ids=frozenset(),
        )
        app = create_app(cfg, db=db, http_client=http_client)
        c = TestClient(app, follow_redirects=False)
        r = c.get("/login/discord")
        assert r.status_code == 303
        assert r.headers["location"] == "/login?error=Discord+OAuth+is+not+configured"

    def test_superadmin_login_success(self, db, http_client) -> None:
        from dashboard.config import DashboardConfig

        cfg = DashboardConfig(
            port=8000,
            secret_key="k" * 32,
            database_path=":memory:",
            file_provider_base_url="",
            discord_client_id="",
            discord_client_secret="",
            discord_redirect_uri="",
            superadmin_password="super-secret",
            admin_user_ids=frozenset(),
        )
        app = create_app(cfg, db=db, http_client=http_client)
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"password": "super-secret"})
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard"
        assert "tvbot_session" in r.cookies

    def test_superadmin_login_wrong_password(self, db, http_client) -> None:
        from dashboard.config import DashboardConfig

        cfg = DashboardConfig(
            port=8000,
            secret_key="k" * 32,
            database_path=":memory:",
            file_provider_base_url="",
            discord_client_id="",
            discord_client_secret="",
            discord_redirect_uri="",
            superadmin_password="super-secret",
            admin_user_ids=frozenset(),
        )
        app = create_app(cfg, db=db, http_client=http_client)
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"password": "wrong-password"})
        assert r.status_code == 303
        assert r.headers["location"] == "/login?error=Invalid+password"
        assert "tvbot_session" not in r.cookies

    def test_superadmin_login_not_configured(self, db, http_client) -> None:
        from dashboard.config import DashboardConfig

        cfg = DashboardConfig(
            port=8000,
            secret_key="k" * 32,
            database_path=":memory:",
            file_provider_base_url="",
            discord_client_id="",
            discord_client_secret="",
            discord_redirect_uri="",
            superadmin_password="",
            admin_user_ids=frozenset(),
        )
        app = create_app(cfg, db=db, http_client=http_client)
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"password": "any"})
        assert r.status_code == 303
        assert r.headers["location"] == "/login?error=Superadmin+login+not+configured"
        assert "tvbot_session" not in r.cookies


# ----------------------------------------------------------------- callback
def _make_client_with_handler(config, db, handler) -> TestClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(config, db=db, http_client=http)
    return TestClient(app, follow_redirects=False)


class TestCallback:
    def test_success_sets_session_for_admin(self, config, db, signer) -> None:
        state_cookie = signer.encode({"state": "st4t3"})

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            if "users/@me" in str(request.url):
                return httpx.Response(
                    200, json={"id": "111", "username": "Alice", "global_name": "Ali"}
                )
            return httpx.Response(500)

        c = _make_client_with_handler(config, db, handler)
        r = c.get(
            "/callback?code=abc&state=st4t3",
            cookies={"tvbot_oauth_state": state_cookie},
        )
        assert r.status_code == 307
        assert r.headers["location"] == "/dashboard"
        assert "tvbot_session" in r.cookies

    def test_non_admin_rejected(self, config, db, signer) -> None:
        state_cookie = signer.encode({"state": "st4t3"})

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"id": "999", "username": "Rando"})

        c = _make_client_with_handler(config, db, handler)
        r = c.get(
            "/callback?code=abc&state=st4t3",
            cookies={"tvbot_oauth_state": state_cookie},
        )
        assert r.status_code == 200  # renders login page with error
        assert "not on the admin whitelist" in r.text

    def test_bad_state_rejected(self, config, db) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="should not be called")

        c = _make_client_with_handler(config, db, handler)
        r = c.get("/callback?code=abc&state=wrong")
        assert r.status_code == 200
        assert "invalid state" in r.text

    def test_provider_error(self, config, db, signer) -> None:
        state_cookie = signer.encode({"state": "st4t3"})

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad")

        c = _make_client_with_handler(config, db, handler)
        r = c.get(
            "/callback?code=abc&state=st4t3",
            cookies={"tvbot_oauth_state": state_cookie},
        )
        assert r.status_code == 200
        assert "authentication failed" in r.text

    def test_missing_code(self, config, db) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        c = _make_client_with_handler(config, db, handler)
        r = c.get("/callback?state=x")
        assert r.status_code == 200
        assert "missing code or state" in r.text


# ------------------------------------------------------------------ pages
class TestDashboardPage:
    def test_renders(self, client: TestClient, admin_cookie: dict) -> None:
        r = client.get("/dashboard", cookies=admin_cookie)
        assert r.status_code == 200
        assert "Now Playing" in r.text
        assert "Currently watching" in r.text
        assert "AdminUser" in r.text

    def test_shows_watchers(self, client: TestClient, admin_cookie: dict, db: Database) -> None:
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at) VALUES(?,?,?)",
            ("u1", "Alice", "2024-01-01 00:00:00"),
        )
        r = client.get("/dashboard", cookies=admin_cookie)
        assert "Alice" in r.text


class TestLeaderboardPage:
    def test_renders_alltime(self, client: TestClient, admin_cookie: dict, db: Database) -> None:
        db.execute(
            "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
            "total_seconds_monthly) VALUES('u1', 'Alice', 3600, 0)",
        )
        r = client.get("/leaderboard", cookies=admin_cookie)
        assert r.status_code == 200
        assert "Alice" in r.text
        assert "1h" in r.text

    def test_monthly_period(self, client: TestClient, admin_cookie: dict) -> None:
        r = client.get("/leaderboard?period=monthly", cookies=admin_cookie)
        assert r.status_code == 200

    def test_invalid_period_defaults(self, client: TestClient, admin_cookie: dict) -> None:
        r = client.get("/leaderboard?period=weekly", cookies=admin_cookie)
        assert r.status_code == 200


class TestQueuePage:
    def test_shows_error_when_provider_unreachable(
        self, client: TestClient, admin_cookie: dict
    ) -> None:
        # The MockTransport in conftest returns 500 for everything → error path.
        r = client.get("/queue", cookies=admin_cookie)
        assert r.status_code == 200
        assert "could not reach file provider" in r.text


# ------------------------------------------------------------------ controls
class TestControls:
    def test_valid_action_enqueued(
        self, client: TestClient, admin_cookie: dict, db: Database
    ) -> None:
        r = client.post(
            "/controls",
            data={"action": "skip", "csrf": "csrf-test"},
            cookies=admin_cookie,
        )
        assert r.status_code == 303
        rows = db.fetchall("SELECT * FROM dashboard_commands")
        assert len(rows) == 1
        assert rows[0]["command"] == "skip"
        assert rows[0]["requested_by"] == "111"

    def test_invalid_csrf_rejected(
        self, client: TestClient, admin_cookie: dict, db: Database
    ) -> None:
        r = client.post(
            "/controls",
            data={"action": "skip", "csrf": "wrong"},
            cookies=admin_cookie,
        )
        assert r.status_code == 403
        assert db.fetchall("SELECT * FROM dashboard_commands") == []

    def test_unknown_action_rejected(
        self, client: TestClient, admin_cookie: dict, db: Database
    ) -> None:
        r = client.post(
            "/controls",
            data={"action": "drop_tables", "csrf": "csrf-test"},
            cookies=admin_cookie,
        )
        assert r.status_code == 400


# ------------------------------------------------------------------ logout
class TestLogout:
    def test_clears_cookie(self, client: TestClient, admin_cookie: dict) -> None:
        r = client.get("/logout", cookies=admin_cookie)
        assert r.status_code == 307
        # cookie deletion sends set-cookie with Max-Age=0
        setc = r.headers.get("set-cookie", "")
        assert "tvbot_session" in setc


class TestQueuePlaylistControls:
    def test_play_now_enqueues_track_command(
        self, client: TestClient, admin_cookie: dict, db: Database
    ) -> None:
        response = client.post(
            "/queue/play",
            data={"track_id": "chosen", "csrf": "csrf-test", "page": "2", "q": "abc"},
            cookies=admin_cookie,
        )
        assert response.status_code == 303
        assert "page=2" in response.headers["location"]
        row = db.fetchone("SELECT command, payload FROM dashboard_commands")
        assert row["command"] == "play_track"
        assert '"chosen"' in row["payload"]

    def test_play_now_requires_csrf(self, client: TestClient, admin_cookie: dict) -> None:
        response = client.post(
            "/queue/play", data={"track_id": "chosen", "csrf": "wrong"}, cookies=admin_cookie
        )
        assert response.status_code == 403
