"""HTTP client for the File Provider service.

The client is intentionally small and typed. The bot uses it like:

    async with FileProviderClient(base_url) as fp:
        track = await fp.current()
        # ... play track.local_path ...
        next_track = await fp.next()

All methods return `TrackResponse` (matching blueprint §4.1) or raise
`ProviderError` / `ProviderUnavailable`. The client transparently retries
transient network failures a few times with a short backoff.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- exceptions
class ProviderError(RuntimeError):
    """Provider responded but with a non-success payload."""


class ProviderUnavailable(ProviderError):
    """Provider is unreachable / returning 5xx after retries."""


# ------------------------------------------------------------------ response
@dataclass(slots=True)
class TrackResponse:
    """Standardized track response per blueprint §4.1."""

    track_id: str
    title: str
    duration_seconds: int
    local_path: str
    provider_used: str
    playlist_position: int
    ready: bool
    has_video: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TrackResponse:
        try:
            return cls(
                track_id=str(data["track_id"]),
                title=str(data["title"]),
                duration_seconds=int(data.get("duration_seconds") or 0),
                local_path=str(data["local_path"]),
                provider_used=str(data.get("provider_used") or "unknown"),
                playlist_position=int(data.get("playlist_position") or 0),
                ready=bool(data.get("ready", True)),
                has_video=bool(data.get("has_video", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed track response: {data!r}") from exc


# -------------------------------------------------------------------- client
class FileProviderClient:
    """Async HTTP client for the file-provider service.

    Parameters
    ----------
    base_url:
        e.g. ``http://file-provider:8001`` — no trailing slash required.
    timeout:
        Per-request timeout in seconds. Downloads happen server-side inside
        the provider, so a small timeout is fine here.
    max_retries:
        Number of extra attempts on transient failures (network errors, 5xx).
        The initial call counts as attempt 1.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> FileProviderClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
            self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- internal
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
            self._owns_client = True
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request with retry-on-transient-failure."""
        client = self._ensure_client()
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await client.request(method, path, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                log.warning("provider %s %s attempt %d failed: %s", method, path, attempt, exc)
            else:
                if 500 <= resp.status_code < 600:
                    last_exc = ProviderUnavailable(f"{method} {path} -> HTTP {resp.status_code}")
                    log.warning(
                        "provider %s %s attempt %d got %d", method, path, attempt, resp.status_code
                    )
                else:
                    return resp
            if attempt < self.max_retries:
                # Exponential-ish backoff: 0.1, 0.2, 0.4 ...
                await asyncio.sleep(0.1 * (2 ** (attempt - 1)))
        raise ProviderUnavailable(str(last_exc) if last_exc else "provider unavailable")

    async def _get_track(self, path: str) -> TrackResponse:
        resp = await self._request("GET", path)
        if resp.status_code != 200:
            raise ProviderError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"non-JSON response from {path}: {resp.text[:200]}") from exc
        return TrackResponse.from_json(data)

    # ------------------------------------------------------------ public API
    async def current(self) -> TrackResponse:
        """Return the track at the current playlist position."""
        return await self._get_track("/current")

    async def next(self) -> TrackResponse:
        """Advance playlist position and return the new current track."""
        resp = await self._request("POST", "/next")
        if resp.status_code != 200:
            raise ProviderError(f"POST /next -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"non-JSON response from /next: {resp.text[:200]}") from exc
        return TrackResponse.from_json(data)

    async def peek(self, count: int = 5) -> list[TrackResponse]:
        """Peek `count` upcoming tracks without advancing (for /queue view)."""
        if count <= 0:
            return []
        resp = await self._request("GET", "/peek", params={"count": count})
        if resp.status_code != 200:
            raise ProviderError(f"GET /peek -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"non-JSON /peek response: {resp.text[:200]}") from exc
        if not isinstance(data, list):
            raise ProviderError(f"/peek expected list, got {type(data).__name__}")
        return [TrackResponse.from_json(item) for item in data]

    async def health(self) -> dict[str, Any]:
        """GET /health — returns provider health map. Never raises for 4xx."""
        resp = await self._request("GET", "/health")
        if resp.status_code >= 400:
            raise ProviderError(f"GET /health -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError("non-JSON /health response") from exc

    async def get_by_id(self, track_id: str) -> TrackResponse:
        """Force-fetch a specific track by id (used by pause/resume)."""
        return await self._get_track(f"/tracks/{track_id}")

    async def list_tracks(
        self, *, offset: int = 0, limit: int = 100, search: str | None = None
    ) -> tuple[list[TrackResponse], int]:
        """List a page of tracks without forcing downloads."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if search:
            params["q"] = search
        resp = await self._request("GET", "/tracks", params=params)
        if resp.status_code != 200:
            raise ProviderError(f"GET /tracks -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise TypeError("missing items")
            return [TrackResponse.from_json(item) for item in data["items"]], int(
                data.get("total") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError("malformed /tracks response") from exc

    async def jump_to(self, track_id: str) -> TrackResponse:
        """Move the provider cursor to a selected track and fetch it."""
        resp = await self._request("POST", f"/jump/{track_id}")
        if resp.status_code != 200:
            raise ProviderError(
                f"POST /jump/{track_id} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return TrackResponse.from_json(resp.json())
        except ValueError as exc:
            raise ProviderError("non-JSON /jump response") from exc

    async def mark_played(self, track_id: str) -> None:
        """Tell the provider a track finished (may be used for pre-fetch/stats)."""
        resp = await self._request("POST", f"/tracks/{track_id}/played")
        # 404 is acceptable — provider may not care.
        if resp.status_code >= 500:  # pragma: no cover — retry already handled it
            raise ProviderError(f"POST /tracks/{track_id}/played -> HTTP {resp.status_code}")
