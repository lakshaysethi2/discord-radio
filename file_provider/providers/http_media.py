"""HTTP media backend for rclone/nginx-style directory listings.

Reads ``HTTP_MEDIA_BASE_URL`` (required to enable), optional
``HTTP_MEDIA_USER`` / ``HTTP_MEDIA_PASSWORD`` for HTTP Basic auth, and
``HTTP_MEDIA_TIMEOUT``.

The provider scans an HTTP directory listing (the HTML index pages that rclone
``serve http`` and nginx autoindex produce) for playable audio/video files,
then downloads them on demand into the shared cache.

Credentials are never logged.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from file_provider.media_types import PLAYABLE_EXTS, is_video_ext
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)

# Regex to extract hrefs from simple directory listings produced by
# rclone serve http and nginx autoindex.
# Matches <a href="...">...</a> and captures the href value.
_HREF_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"', re.IGNORECASE)

# Extensions we skip when scanning (directories, metadata, images, etc.).
_SKIP_EXTS: frozenset[str] = frozenset({
    ".html", ".htm", ".json", ".xml", ".txt", ".md", ".pdf",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".eot",
})


def _is_playable_link(href: str) -> bool:
    """True if the href looks like a playable media file."""
    # Skip parent-directory links and directories (end with /)
    if href in (".", "..", "/"):
        return False
    if href.endswith("/"):
        return False
    _lower = href.lower()
    _, ext = os.path.splitext(_lower)
    if not ext:
        return False
    if ext in _SKIP_EXTS:
        return False
    return ext in PLAYABLE_EXTS


def _is_dir_link(href: str) -> bool:
    """True if the href looks like a subdirectory.

    Skips parent-directory links (.., ../) to prevent infinite recursion.
    """
    if href in (".", "..", "/"):
        return False
    if href in ("./", "../"):
        return False
    return href.endswith("/")


def _hostname_from_url(url: str) -> str:
    """Extract hostname for logging without leaking credentials in the URL."""
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


class HttpMediaProvider(BaseProvider):
    """Backend that streams from a private HTTP media library.

    Expects rclone ``serve http`` or nginx autoindex directory listing
    at the configured base URL. Files are downloaded on demand.
    """

    name = "http"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        http_timeout: float = 60.0,
        recursive: bool = True,
        path_prefix: str = "",
        download_chunk_bytes: int = 64 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.username = username or ""
        self.password = password or ""
        self.http_timeout = http_timeout
        self.recursive = recursive
        self.path_prefix = path_prefix
        self.download_chunk_bytes = download_chunk_bytes

        # Cached listing tree: { "path/to/file.mp3": full_url, ... }
        self._listing: dict[str, str] = {}

    # ----------------------------------------------------------- helpers
    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _client(self) -> httpx.Client:
        auth: httpx.BasicAuth | None = None
        if self.username or self.password:
            auth = httpx.BasicAuth(username=self.username, password=self.password)

        # Redact password from logs
        safe_host = _hostname_from_url(self.base_url)
        return httpx.Client(
            timeout=self.http_timeout,
            auth=auth,
            follow_redirects=True,
            headers={"User-Agent": "discord-radio/http-media-provider"},
            # Log the host but not credentials or path
            event_hooks={
                "request": [
                    lambda request: log.debug(
                        "HTTP media provider %s %s on %s",
                        request.method,
                        request.url.path,
                        safe_host,
                    )
                ],
            },
        )

    # ------------------------------------------------------------ scan
    def list_tracks(self) -> list[ProviderTrack]:
        if not self.is_configured():
            log.info("HTTP media provider: no HTTP_MEDIA_BASE_URL configured")
            return []

        self._listing = {}

        # Parse base URL path so we can enforce directory boundary.
        base_path = urlparse(self.base_url).path.rstrip("/") + "/"
        visited: set[str] = set()

        try:
            urls = self._scan_url(
                self.base_url,
                base_path=base_path,
                prefix=self.path_prefix,
                visited=visited,
            )
        except Exception as exc:
            log.warning(
                "HTTP media provider: scan of %s failed: %s",
                _hostname_from_url(self.base_url), exc,
            )
            return []

        if not urls:
            log.info(
                "HTTP media provider: no playable files found at %s",
                _hostname_from_url(self.base_url),
            )
            return []

        # Convert URL map to sorted ProviderTrack list.
        tracks: list[ProviderTrack] = []
        for path_sorted in sorted(urls.keys()):
            title = Path(path_sorted).stem
            ext = os.path.splitext(path_sorted.lower())[1]
            tracks.append(
                ProviderTrack(
                    title=title,
                    source_ref=path_sorted,
                    duration_seconds=0,
                    size_bytes=0,
                    has_video=is_video_ext(ext),
                )
            )

        log.info(
            "HTTP media provider: found %d playable files at %s",
            len(tracks), _hostname_from_url(self.base_url),
        )
        return tracks

    def _scan_url(
        self,
        url: str,
        *,
        base_path: str = "/",
        prefix: str = "",
        depth: int = 0,
        visited: set[str] | None = None,
    ) -> dict[str, str]:
        """Recursively scan an HTTP directory listing.

        Returns a dict of ``{relative_path: full_url}`` for playable files.

        Uses a visited set to avoid re-entering directories (which causes
        infinite loops when listings contain absolute links like ``/Inbox/``
        or parent-directory links).  Only recurses into hrefs whose
        absolute URL stays under *base_path* on the same host.
        """
        if visited is None:
            visited = set()

        # Normalize: ensure trailing slash, strip fragment/query
        scan_url = url.rstrip("/") + "/"

        # Skip already-visited directories (canonicalized by trailing-slash form).
        if scan_url in visited:
            return {}
        visited.add(scan_url)

        if depth > 10:
            log.warning(
                "HTTP media scan: max depth (%d) reached at %s",
                10, _hostname_from_url(scan_url) + urlparse(scan_url).path,
            )
            return {}

        result: dict[str, str] = {}

        safe_host = _hostname_from_url(scan_url)
        try:
            with self._client() as client:
                resp = client.get(scan_url, timeout=self.http_timeout)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            log.warning(
                "HTTP media scan: failed to read %s%s: %s",
                safe_host, urlparse(scan_url).path, exc,
            )
            return result

        links = _HREF_RE.findall(html)

        for href in links:
            # Skip parent-directory and self-referencing links.
            if href in (".", "..", "/", "./", "../"):
                continue
            if "/../" in href or href.startswith("../"):
                continue
            if ".." in href.split("/"):
                continue

            full_url = urljoin(scan_url, href)

            if _is_dir_link(href):
                # Only recurse if the resolved URL stays under base_path.
                if not self.recursive:
                    continue
                parsed_child = urlparse(full_url)
                child_path = parsed_child.path.rstrip("/") + "/"
                # Skip if the child path does not start with base_path.
                if not child_path.startswith(base_path):
                    continue
                dir_name = unquote(href.rstrip("/"))
                sub_prefix = (prefix + "/" + dir_name) if prefix else dir_name
                sub_result = self._scan_url(
                    full_url,
                    base_path=base_path,
                    prefix=sub_prefix,
                    depth=depth + 1,
                    visited=visited,
                )
                result.update(sub_result)
            elif _is_playable_link(href):
                decoded_href = unquote(href)
                rel_path = (prefix + "/" + decoded_href) if prefix else decoded_href
                rel_path = rel_path.lstrip("/")
                result[rel_path] = full_url

        return result

    # ----------------------------------------------------------- fetch
    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        if not self.base_url:
            raise ProviderFetchError("HTTP media provider not configured (no base URL)")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path

        # Build download URL from the listing, or fall back to appending source_ref
        file_url = self._listing.get(source_ref)
        if file_url is None:
            file_url = urljoin(self.base_url.rstrip("/") + "/", source_ref)

        partial = target_path.with_suffix(target_path.suffix + ".part")
        # Clean up any half-written file from a previous failed attempt.
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass

        try:
            with self._client() as client, client.stream("GET", file_url) as resp:
                if resp.status_code >= 400:
                    raise ProviderFetchError(
                        f"HTTP media GET {_hostname_from_url(file_url)} "
                        f"-> HTTP {resp.status_code}"
                    )
                with open(partial, "wb") as f:
                    for chunk in resp.iter_bytes(self.download_chunk_bytes):
                        if chunk:
                            f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(partial, target_path)
        except ProviderFetchError:
            self._cleanup_partial(partial)
            raise
        except Exception as exc:
            self._cleanup_partial(partial)
            raise ProviderFetchError(
                f"HTTP media fetch failed from {_hostname_from_url(file_url)}: {exc}"
            ) from exc

        return target_path

    @staticmethod
    def _cleanup_partial(path: Path) -> None:
        """Remove a partial file, ignoring OSError."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
