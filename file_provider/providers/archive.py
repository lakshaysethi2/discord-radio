"""archive.org (Internet Archive) backend.

For each configured item id, we call the public metadata API to enumerate its
audio files, then download files on demand from ``archive.org/download/...``.

No auth required. Every archive.org item exposes:

    GET https://archive.org/metadata/<item_id>
        → JSON { "files": [ {"name":"...", "size":"...", "length":"...",
                             "format":"VBR MP3", "source":"original"}, ... ] }

    GET https://archive.org/download/<item_id>/<url-escaped file name>
        → the actual file. Supports HTTP range. Public.

Only ``source: original`` audio files are surfaced — the API also returns
generated derivatives (spectrograms, waveform PNGs, Columbia Peaks .afpk)
that we don't want in the playlist.

Point ``ARCHIVE_ORG_ITEMS`` at one or more item ids (comma-separated). E.g.:

    ARCHIVE_ORG_ITEMS=Hawkins_Lectures_transcoded_actual_files
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

import httpx

from file_provider.media_types import (
    PLAYABLE_ARCHIVE_FORMATS,
    PLAYABLE_EXTS,
    VIDEO_ARCHIVE_FORMATS,
    is_video_ext,
)
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)

METADATA_URL = "https://archive.org/metadata/{item_id}"
DOWNLOAD_URL = "https://archive.org/download/{item_id}/{path}"

# Kept for backward-compat with anything that used to import from here.
AUDIO_FORMATS = PLAYABLE_ARCHIVE_FORMATS
AUDIO_EXTS = PLAYABLE_EXTS


class ArchiveOrgProvider(BaseProvider):
    """Backend that streams from public archive.org items."""

    name = "archive"

    def __init__(
        self,
        item_ids: list[str],
        *,
        http_timeout: float = 60.0,
        download_chunk_bytes: int = 64 * 1024,
        user_agent: str = "discord-radio/1.0 (+https://github.com/lakshaysethi2/discord-radio)",
    ) -> None:
        self.item_ids = [i.strip() for i in item_ids if i.strip()]
        self.http_timeout = http_timeout
        self.download_chunk_bytes = download_chunk_bytes
        self.user_agent = user_agent

    # ----------------------------------------------------------- helpers
    def is_configured(self) -> bool:
        return bool(self.item_ids)

    def _client(self) -> httpx.Client:
        # A short-lived client per call keeps things simple and avoids weird
        # connection reuse across long-lived Telethon-style patterns.
        return httpx.Client(
            timeout=self.http_timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )

    # ------------------------------------------------------------ scan
    def list_tracks(self) -> list[ProviderTrack]:
        if not self.is_configured():
            log.info("archive.org provider: no ARCHIVE_ORG_ITEMS configured")
            return []
        tracks: list[ProviderTrack] = []
        with self._client() as c:
            for item_id in self.item_ids:
                try:
                    tracks.extend(self._scan_item(c, item_id))
                except Exception as exc:
                    log.warning("archive.org: item %s scan failed: %s", item_id, exc)
        # Stable ordering: item id first, then file name.
        tracks.sort(key=lambda t: t.source_ref)
        log.info(
            "archive.org: found %d audio files across %d items", len(tracks), len(self.item_ids)
        )
        return tracks

    def _scan_item(self, c: httpx.Client, item_id: str) -> list[ProviderTrack]:
        url = METADATA_URL.format(item_id=item_id)
        r = c.get(url)
        r.raise_for_status()
        data = r.json()
        files = data.get("files") or []
        out: list[ProviderTrack] = []
        for f in files:
            if not self._is_playable(f):
                continue
            name = f.get("name") or ""
            if not name:
                continue
            title = self._title_for(name)
            try:
                size = int(f.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            duration = self._duration_of(f)
            # source_ref packs both item id and path so we can re-download later.
            source_ref = f"{item_id}::{name}"
            out.append(
                ProviderTrack(
                    title=title,
                    source_ref=source_ref,
                    duration_seconds=duration,
                    size_bytes=size,
                    has_video=self._is_video(f),
                )
            )
        return out

    @staticmethod
    def _is_playable(f: dict) -> bool:
        """True for audio OR video containers (source=original only).

        Video files with audio tracks are playable — FFmpeg strips the video
        when streaming to Discord voice.
        """
        # Prefer explicit "source: original" so we don't pick up derivatives
        # (spectrograms, previews, .afpk files).
        if f.get("source") != "original":
            return False
        fmt = (f.get("format") or "").strip()
        if fmt in PLAYABLE_ARCHIVE_FORMATS:
            return True
        name = f.get("name") or ""
        _, ext = os.path.splitext(name.lower())
        return ext in PLAYABLE_EXTS

    @staticmethod
    def _is_video(f: dict) -> bool:
        fmt = (f.get("format") or "").strip()
        if fmt in VIDEO_ARCHIVE_FORMATS:
            return True
        name = f.get("name") or ""
        _, ext = os.path.splitext(name.lower())
        return is_video_ext(ext)

    @staticmethod
    def _title_for(name: str) -> str:
        """Turn 'BTO Radio Interviews/#03 - 04_25_02 - #9B58.mp3' into a title.

        Prefers the leaf filename minus extension, but also prepends the last
        directory segment if the filename is generic (e.g. 'track01.mp3').
        """
        parts = name.replace("\\", "/").split("/")
        leaf = parts[-1]
        stem, _ext = os.path.splitext(leaf)
        stem = stem.strip()

        # Heuristic: generic-looking names get their parent dir prepended for
        # context ("Lecture 1 · Track01").
        looks_generic = stem.lower() in {"", "track01", "audio", "part1", "part-1"} or (
            len(stem) < 6 and stem.replace("-", "").replace("_", "").isdigit()
        )
        if looks_generic and len(parts) >= 2:
            return f"{parts[-2]} · {stem}"
        # Otherwise use the filename directly; if it's very generic AND lives in
        # a nested dir, include one level of context for browsability.
        if len(parts) >= 2 and len(stem) < 40:
            return f"{parts[-2]} — {stem}"
        return stem or leaf

    @staticmethod
    def _duration_of(f: dict) -> int:
        # archive.org reports length as either seconds (float string) or
        # 'HH:MM:SS'. Handle both.
        raw = f.get("length")
        if raw is None:
            return 0
        s = str(raw).strip()
        if not s:
            return 0
        if ":" in s:
            try:
                parts = [float(p) for p in s.split(":")]
            except ValueError:
                return 0
            total = 0.0
            for p in parts:
                total = total * 60 + p
            return int(total)
        try:
            return int(float(s))
        except ValueError:
            return 0

    # ----------------------------------------------------------- fetch
    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        if "::" not in source_ref:
            raise ProviderFetchError(f"malformed archive source_ref: {source_ref!r}")
        item_id, path = source_ref.split("::", 1)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path

        # We escape each path segment separately so slashes stay as-is.
        from urllib.parse import quote

        safe_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
        url = DOWNLOAD_URL.format(item_id=item_id, path=safe_path)

        partial = target_path.with_suffix(target_path.suffix + ".part")
        # Clean up any half-written file from a previous failed attempt.
        with contextlib.suppress(OSError):
            if partial.exists():
                partial.unlink()

        try:
            with self._client() as c, c.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ProviderFetchError(f"archive.org GET {url} → HTTP {resp.status_code}")
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
            raise ProviderFetchError(f"archive.org fetch failed: {exc}") from exc

        return target_path
