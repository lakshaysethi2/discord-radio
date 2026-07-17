"""FastAPI app wiring (§11).

Routes:
    GET  /                          → redirect to /dashboard
    GET  /login                     → start Discord OAuth2 flow
    GET  /callback                  → OAuth2 callback (code exchange)
    GET  /dashboard                 → now playing + watchers + controls
    GET  /leaderboard?period=…      → ranked totals
    GET  /queue                     → upcoming tracks (via file provider)
    POST /controls                  → single POST handler (form field `action`)
    GET  /logout                    → clear session

Design notes:

* Sessions are signed cookies (itsdangerous) — no server-side session store,
  which keeps the dashboard stateless and simple.
* CSRF: every POST /controls form includes a `csrf` field mirrored from the
  session cookie's `csrf` value. Constant-time compare.
* Admin allowlist: user id must be in ADMIN_USER_IDS at login time; further
  page loads simply require a valid session cookie.
* The dashboard writes to `dashboard_commands` — the bot polls & executes.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from dashboard import auth, commands, queries
from dashboard.config import DashboardConfig, load
from db.database import Database
from db.models import BotStateKey
from provider.client import FileProviderClient

log = logging.getLogger(__name__)


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["hms"] = queries.format_hms
    return templates


def create_app(
    config: DashboardConfig | None = None,
    *,
    db: Database | None = None,
    provider: FileProviderClient | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the FastAPI application. Everything is injectable for tests."""
    config = config or load()

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # Shutdown: close lazily-created httpx client + DB.
        with contextlib.suppress(Exception):
            if _app.state.provider is not None:
                await _app.state.provider.aclose()
        with contextlib.suppress(Exception):
            _app.state.db.close()

    app = FastAPI(title="Discord TV Bot Dashboard", version="0.1.0", lifespan=_lifespan)

    templates = _build_templates()
    signer = auth.SessionSigner(config.secret_key)

    # Shared instances stashed on app.state for teardown.
    app.state.config = config
    app.state.db = db or Database(config.database_path)
    app.state.signer = signer
    app.state.templates = templates
    app.state.provider = provider  # may be None; created lazily on /queue
    app.state.http = http_client  # optional httpx client for OAuth2 (tests inject)

    # ------------------------------------------------------------ helpers
    def _get_session(request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if not token:
            return None
        return signer.decode(token)

    def _require_admin(request: Request) -> auth.SessionUser:
        sess = _get_session(request)
        if not sess or "user_id" not in sess:
            # Redirect to login. 303 makes browsers switch to GET.
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        return auth.SessionUser(user_id=sess["user_id"], username=sess.get("username", "?"))

    def _http() -> httpx.AsyncClient:
        # Prefer an injected client (tests); otherwise create a per-request one.
        return app.state.http or httpx.AsyncClient(timeout=10.0)

    async def _get_provider() -> FileProviderClient:
        if app.state.provider is not None:
            return app.state.provider
        # Lazy: build once and cache.
        app.state.provider = FileProviderClient(config.file_provider_base_url)
        return app.state.provider

    def _render(request: Request, name: str, ctx: dict[str, Any]) -> HTMLResponse:
        user = ctx.get("user")
        if user is None:
            sess = _get_session(request)
            if sess:
                user = auth.SessionUser(user_id=sess["user_id"], username=sess.get("username", "?"))
        base_ctx = {"request": request, "user": user, "config": config}
        base_ctx.update(ctx)
        return templates.TemplateResponse(request, name, base_ctx)

    # ------------------------------------------------------------- routes
    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=307)

    # ---- Authentication ----
    @app.get("/login")
    def login(request: Request) -> Response:
        return _render(
            request,
            "login.html",
            {
                "oauth_configured": config.oauth_configured,
                "superadmin_configured": bool(config.superadmin_password),
                "error": request.query_params.get("error"),
            },
        )

    @app.get("/login/discord")
    def login_discord(request: Request) -> Response:
        if not config.oauth_configured:
            return RedirectResponse("/login?error=Discord+OAuth+is+not+configured", status_code=303)
        state = auth.generate_state()
        url = auth.build_login_url(
            client_id=config.discord_client_id,
            redirect_uri=config.discord_redirect_uri,
            state=state,
        )
        resp = RedirectResponse(url, status_code=307)
        # Keep the state in a signed cookie so we can verify on callback.
        resp.set_cookie(
            auth.OAUTH_STATE_COOKIE,
            signer.encode({"state": state}),
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp

    @app.post("/login")
    def login_post(
        request: Request,
        password: str = Form(""),
    ) -> Response:
        if not config.superadmin_password:
            return RedirectResponse("/login?error=Superadmin+login+not+configured", status_code=303)

        if password != config.superadmin_password:
            return RedirectResponse("/login?error=Invalid+password", status_code=303)

        session_data = {
            "user_id": "superadmin",
            "username": "Superadmin",
            "csrf": secrets.token_urlsafe(24),
        }
        resp = RedirectResponse("/dashboard", status_code=303)
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            signer.encode(session_data),
            max_age=auth.COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp

    @app.get("/callback")
    async def callback(
        request: Request,
        code: str | None = Query(None),
        state: str | None = Query(None),
        error: str | None = Query(None),
    ) -> Response:
        if error:
            return _render(request, "login.html", {"oauth_configured": True, "error": error})
        if not code or not state:
            return _render(
                request,
                "login.html",
                {"oauth_configured": True, "error": "missing code or state"},
            )

        # Validate state (CSRF for the OAuth flow itself).
        state_cookie = request.cookies.get(auth.OAUTH_STATE_COOKIE)
        expected = signer.decode(state_cookie) if state_cookie else None
        if not expected or not hmac.compare_digest(expected.get("state", ""), state):
            return _render(
                request,
                "login.html",
                {"oauth_configured": True, "error": "invalid state — try again"},
            )

        http = _http()
        owns_http = http is not app.state.http
        try:
            try:
                token_data = await auth.exchange_code_for_token(
                    code=code,
                    client_id=config.discord_client_id,
                    client_secret=config.discord_client_secret,
                    redirect_uri=config.discord_redirect_uri,
                    http=http,
                )
                user_data = await auth.fetch_discord_user(
                    access_token=token_data["access_token"], http=http
                )
            except auth.OAuthError as exc:
                log.warning("OAuth failed: %s", exc)
                return _render(
                    request,
                    "login.html",
                    {"oauth_configured": True, "error": "authentication failed"},
                )
        finally:
            if owns_http:
                await http.aclose()

        user_id = str(user_data.get("id", ""))
        username = user_data.get("global_name") or user_data.get("username") or "?"

        if not auth.is_admin(user_id, config.admin_user_ids):
            return _render(
                request,
                "login.html",
                {
                    "oauth_configured": True,
                    "error": f"user {username} is not on the admin whitelist",
                },
            )

        session_data = {
            "user_id": user_id,
            "username": username,
            "csrf": secrets.token_urlsafe(24),
        }
        resp = RedirectResponse("/dashboard", status_code=307)
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            signer.encode(session_data),
            max_age=auth.COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        resp.delete_cookie(auth.OAUTH_STATE_COOKIE)
        return resp

    @app.get("/logout")
    def logout() -> Response:
        resp = RedirectResponse("/", status_code=307)
        resp.delete_cookie(auth.SESSION_COOKIE_NAME)
        return resp

    # ---- Pages ----
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(
        request: Request, user: auth.SessionUser = Depends(_require_admin)
    ) -> HTMLResponse:
        db = app.state.db
        np = queries.now_playing(db)
        watchers = queries.current_watchers(db)

        # Try to enrich Now Playing with a title via provider. If the provider
        # is down we still render the page — dashboard reliability shouldn't
        # depend on it.
        track_title = None
        track_duration = 0
        if np.track_id:
            try:
                fp = await _get_provider()
                track = await fp.get_by_id(np.track_id)
                track_title = track.title
                track_duration = track.duration_seconds
            except Exception as exc:
                log.debug("could not fetch current track from provider: %s", exc)

        sess = _get_session(request) or {}
        return _render(
            request,
            "dashboard.html",
            {
                "user": user,
                "now": np,
                "watchers": watchers,
                "track_title": track_title,
                "track_duration": track_duration,
                "elapsed_fmt": queries.format_hms(np.playback_position_seconds),
                "duration_fmt": queries.format_hms(track_duration),
                "pending": commands.pending(db),
                "csrf": sess.get("csrf", ""),
                "command_flash": request.query_params.get("flash"),
                "stream_volume_percent": db.get_state_int(BotStateKey.STREAM_VOLUME_PERCENT, 100),
            },
        )

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard_page(
        request: Request,
        period: str = Query("alltime"),
        user: auth.SessionUser = Depends(_require_admin),
    ) -> HTMLResponse:
        if period not in ("alltime", "monthly"):
            period = "alltime"
        rows = queries.leaderboard(app.state.db, period=period, limit=100)
        return _render(
            request,
            "leaderboard.html",
            {"user": user, "period": period, "rows": rows},
        )

    @app.get("/queue", response_class=HTMLResponse)
    async def queue_page(
        request: Request,
        user: auth.SessionUser = Depends(_require_admin),
        page: int = Query(1, ge=1, le=10_000),
        page_size: int = Query(50, ge=10, le=500),
        q: str | None = Query(None),
    ) -> HTMLResponse:
        """Paginated playlist with search and a direct play-now control."""
        tracks: list = []
        total = 0
        error: str | None = None
        current_track_id: str | None = None
        current_page: int | None = None
        search = (q or "").strip() or None
        try:
            fp = await _get_provider()
            tracks, total = await fp.list_tracks(
                offset=(page - 1) * page_size, limit=page_size, search=search
            )
        except Exception as exc:
            error = f"could not reach file provider: {exc}"
            log.warning("queue fetch failed: %s", exc)

        # The bot already persists its active track and position in shared
        # SQLite. Reading it here avoids a provider /current request, which
        # intentionally fetches uncached media and would make a browse-only
        # page unexpectedly download a large track.
        current = queries.now_playing(app.state.db)
        current_track_id = current.track_id
        if current_track_id:
            current_page = (current.playlist_position // page_size) + 1
        sess = _get_session(request) or {}
        return _render(
            request,
            "queue.html",
            {
                "user": user,
                "tracks": tracks,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "q": q or "",
                "current_track_id": current_track_id,
                "current_page": current_page,
                "error": error,
                "csrf": sess.get("csrf", ""),
                "command_flash": request.query_params.get("flash"),
            },
        )

    @app.post("/queue/play")
    async def queue_play(
        request: Request,
        track_id: str = Form(...),
        csrf: str = Form(""),
        page: int = Form(1),
        q: str = Form(""),
        user: auth.SessionUser = Depends(_require_admin),
    ) -> Response:
        sess = _get_session(request) or {}
        if not sess.get("csrf") or not hmac.compare_digest(sess["csrf"], csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        commands.enqueue(
            app.state.db,
            command="play_track",
            requested_by=user.user_id,
            payload={"track_id": track_id},
        )
        from urllib.parse import urlencode

        params = {"page": page, "q": q, "flash": "Playing selected track"}
        return RedirectResponse(
            "/queue?" + urlencode({k: v for k, v in params.items() if v}), status_code=303
        )

    @app.post("/controls/volume")
    async def set_volume(
        request: Request,
        volume_percent: int = Form(...),
        csrf: str = Form(""),
        user: auth.SessionUser = Depends(_require_admin),
    ) -> Response:
        sess = _get_session(request) or {}
        if not sess.get("csrf") or not hmac.compare_digest(sess["csrf"], csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        if not 50 <= volume_percent <= 250:
            raise HTTPException(status_code=422, detail="volume must be between 50 and 250")
        commands.enqueue(
            app.state.db,
            command="set_volume",
            requested_by=user.user_id,
            payload={"volume_percent": volume_percent},
        )
        return RedirectResponse(
            f"/dashboard?flash=Queued+volume+{volume_percent}%25", status_code=303
        )

    # ---- Controls ----
    @app.post("/controls")
    async def controls(
        request: Request,
        action: str = Form(...),
        csrf: str = Form(""),
        user: auth.SessionUser = Depends(_require_admin),
    ) -> Response:
        sess = _get_session(request) or {}
        session_csrf = sess.get("csrf", "")
        if not session_csrf or not hmac.compare_digest(session_csrf, csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        try:
            commands.enqueue(app.state.db, command=action, requested_by=user.user_id)
        except commands.UnknownCommandError:
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}") from None
        return RedirectResponse(f"/dashboard?flash=Queued+{action}", status_code=303)

    return app


# ASGI entry — `uvicorn dashboard.main:app`
#
# We build the app lazily via a factory attribute so:
#   * unit tests never trigger a real DB open by importing the module,
#   * uvicorn (which does `from dashboard.main import app`) still gets a
#     ready-to-serve app on first attribute access.
def _default_app() -> FastAPI:
    return create_app()


class _LazyApp:
    """Proxy that materialises the real app on first attribute access."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    def _get(self) -> FastAPI:
        if self._app is None:
            self._app = _default_app()
        return self._app

    def __getattr__(self, item: str) -> Any:
        return getattr(self._get(), item)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # ASGI entrypoint — uvicorn calls this directly.
        await self._get()(scope, receive, send)


app: Any = _LazyApp()
