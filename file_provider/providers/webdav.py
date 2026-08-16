"""WebDAV backend (rclone serve webdav over a Google Drive remote).

The file-provider talks to an rclone WebDAV endpoint, e.g. a sidecar running

    rclone serve webdav gdrive:mother-of-all-torrents --addr :8081 --read-only

Listings happen over HTTP ``PROPFIND`` (Depth: 1, recursed client-side) and
files are streamed over HTTP ``GET`` — so a dead remote marks this provider
unhealthy instead of wedging the process on a stale FUSE mountpoint (the
2026-08-16 outage failure mode).

Env: ``GDRIVE_WEBDAV_URL`` (required), ``GDRIVE_WEBDAV_PATH`` (default "/"),
optional ``GDRIVE_WEBDAV_USER`` / ``GDRIVE_WEBDAV_PASS`` for basic auth.
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from file_provider.media_types import PLAYABLE_EXTS, is_video_ext
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)

DAV = "{DAV:}"


def _dav(tag: str) -> str:
    return f"{DAV}{tag}"


class WebDavProvider(BaseProvider):
    """Backend that streams from an rclone WebDAV serve endpoint."""

    name = "webdav"

    def __init__(
        self,
        url: str,
        path: str = "/",
        *,
        username: str = "",
        password: str = "",
        http_timeout: float = 60.0,
        download_chunk_bytes: int = 64 * 1024,
        user_agent: str = "discord-radio/1.0 (+https://github.com/lakshaysethi2/discord-radio)",
    ) -> None:
        self.url = url.rstrip("/")
        self.path = posixpath.normpath("/" + path.lstrip("/"))
        if self.path == "/.":
            self.path = "/"
        self.username = username
        self.password = password
        self.http_timeout = http_timeout
        self.download_chunk_bytes = download_chunk_bytes
        self.user_agent = user_agent

    # ----------------------------------------------------------- helpers
    def is_configured(self) -> bool:
        return bool(self.url)

    def _client(self) -> httpx.Client:
        auth = (self.username, self.password) if self.username else None
        return httpx.Client(
            timeout=self.http_timeout,
            headers={"User-Agent": self.user_agent},
            auth=auth,
            follow_redirects=True,
        )

    # ------------------------------------------------------------ scan
    def list_tracks(self) -> list[ProviderTrack]:
        if not self.is_configured():
            log.info("webdav provider: no GDRIVE_WEBDAV_URL configured")
            return []
        tracks: list[ProviderTrack] = []
        seen_dirs: set[str] = set()
        with self._client() as c:
            stack = [self.path]
            while stack:
                dir_path = stack.pop()
                if dir_path in seen_dirs:
                    continue
                seen_dirs.add(dir_path)
                try:
                    entries = self._propfind(c, dir_path)
                except Exception as exc:
                    log.warning("webdav: PROPFIND %s failed: %s", dir_path, exc)
                    continue
                for e in entries:
                    if e["is_dir"]:
                        if e["href"] != dir_path:
                            stack.append(e["href"])
                        continue
                    tracks.append(
                        ProviderTrack(
                            title=self._title_for(e["href"]),
                            source_ref=self._relative(e["href"]),
                            size_bytes=e["size"],
                            has_video=is_video_ext(os.path.splitext(e["href"].lower())[1]),
                        )
                    )
        tracks.sort(key=lambda t: t.source_ref)
        log.info("webdav: found %d tracks under %s", len(tracks), self.path)
        return tracks

    def _propfind(self, c: httpx.Client, dir_path: str) -> list[dict]:
        """PROPFIND Depth:1 on `dir_path`; returns href/is_dir/size entries."""
        url = self._href_url(dir_path)
        resp = c.request(
            "PROPFIND",
            url,
            headers={"Depth": "1"},
        )
        if resp.status_code >= 400:
            raise ProviderFetchError(f"PROPFIND {url} → HTTP {resp.status_code}")
        root = ET.fromstring(resp.content)
        out: list[dict] = []
        for resp_el in root.findall(_dav("response")):
            href_el = resp_el.find(_dav("href"))
            if href_el is None or not href_el.text:
                continue
            href = self._norm_href(href_el.text)
            if not self._in_scope(href):
                continue
            is_dir = False
            size = 0
            for prop in resp_el.iter(_dav("resourcetype")):
                if prop.find(_dav("collection")) is not None:
                    is_dir = True
                    break
            if not is_dir:
                ext = os.path.splitext(href.lower())[1]
                if ext not in PLAYABLE_EXTS:
                    continue
                for prop in resp_el.iter(_dav("getcontentlength")):
                    if prop.text:
                        with contextlib.suppress(ValueError):
                            size = int(prop.text.strip())
                        break
            out.append({"href": href, "is_dir": is_dir, "size": size})
        return out

    def _norm_href(self, href: str) -> str:
        """Turn a (possibly absolute/URL-encoded) href into a decoded posix path.

        URL parsing happens before percent-decoding so a '#1' in a filename
        (escaped as %23 in the href) isn't mistaken for a URL fragment.
        """
        path = href
        if "://" in path:
            path = urlsplit(urljoin(self.url + "/", path)).path
        path = unquote(path)
        return posixpath.normpath("/" + path.lstrip("/"))

    def _in_scope(self, href: str) -> bool:
        if self.path == "/":
            return True
        return href == self.path or href.startswith(self.path.rstrip("/") + "/")

    def _relative(self, href: str) -> str:
        if self.path == "/":
            return href.lstrip("/")
        prefix = self.path.rstrip("/") + "/"
        return href[len(prefix) :] if href.startswith(prefix) else href.lstrip("/")

    def _href_url(self, href: str) -> str:
        safe = "/".join(quote(seg, safe="") for seg in href.strip("/").split("/"))
        return f"{self.url}/{safe}"

    @staticmethod
    def _title_for(href: str) -> str:
        leaf = posixpath.basename(href.rstrip("/"))
        stem, _ext = os.path.splitext(leaf)
        return stem.strip() or leaf

    # ----------------------------------------------------------- fetch
    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        href = self._relative_to_href(source_ref)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path

        url = self._href_url(href)
        partial = target_path.with_suffix(target_path.suffix + ".part")
        with contextlib.suppress(OSError):
            if partial.exists():
                partial.unlink()

        try:
            with self._client() as c, c.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ProviderFetchError(f"webdav GET {url} → HTTP {resp.status_code}")
                with open(partial, "wb") as f:
                    for chunk in resp.iter_bytes(self.download_chunk_bytes):
                        if chunk:
                            f.write(chunk)
            os.replace(partial, target_path)
        except ProviderFetchError:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise
        except Exception as exc:  # network, disk, ...
            with contextlib.suppress(OSError):
                partial.unlink()
            raise ProviderFetchError(f"webdav fetch failed: {exc}") from exc

        return target_path

    def _relative_to_href(self, source_ref: str) -> str:
        # Reject traversal — source_refs come from our own DB but be defensive.
        norm = posixpath.normpath("/" + source_ref.lstrip("/"))
        if norm == "/.":
            norm = "/"
        if self.path == "/":
            return norm
        return posixpath.join(self.path, source_ref.lstrip("/"))
