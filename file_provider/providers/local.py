"""Local filesystem provider.

Scans a directory recursively for playable files (audio + video containers)
and serves them by hardlinking (or copying, if a hardlink would cross
filesystems) into the cache.

Video files are accepted too — FFmpeg strips the video track when streaming
to Discord voice (see `bot.player.default_ffmpeg_source`). We flag them with
`has_video=True` so the dashboard can badge them.

Great for dev, tests, and self-hosters who already have a media library on disk.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from file_provider.media_types import PLAYABLE_EXTS, is_video_ext
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)


class LocalProvider(BaseProvider):
    name = "local"

    def __init__(self, media_root: str | os.PathLike[str]) -> None:
        self.media_root = Path(media_root)

    def is_configured(self) -> bool:
        return self.media_root.exists() and self.media_root.is_dir()

    def list_tracks(self) -> list[ProviderTrack]:
        if not self.is_configured():
            log.info("local media root %s does not exist", self.media_root)
            return []
        tracks: list[ProviderTrack] = []
        for path in sorted(self.media_root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext not in PLAYABLE_EXTS:
                continue
            try:
                # Guard against symlinks pointing outside the root.
                rel = path.resolve().relative_to(self.media_root.resolve())
            except ValueError:
                log.debug("skipping %s: outside media root", path)
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            tracks.append(
                ProviderTrack(
                    title=path.stem,
                    source_ref=str(rel),
                    size_bytes=size,
                    has_video=is_video_ext(ext),
                )
            )
        return tracks

    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        # Reject source_refs trying to escape via .. — defence in depth even
        # though source_refs come from our own DB.
        src = (self.media_root / source_ref).resolve()
        try:
            src.relative_to(self.media_root.resolve())
        except ValueError as exc:
            raise ProviderFetchError(f"source_ref {source_ref!r} escapes media root") from exc
        if not src.exists():
            raise ProviderFetchError(f"local file missing: {src}")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            return target_path

        # Try hardlink first (instant, no disk usage), fall back to copy.
        try:
            os.link(src, target_path)
        except OSError:
            try:
                shutil.copy2(src, target_path)
            except OSError as exc:
                raise ProviderFetchError(f"could not copy {src} -> {target_path}: {exc}") from exc
        return target_path
