"""Local filesystem provider.

Scans a directory recursively for audio files and serves them by hardlinking
(or copying, if a hardlink would cross filesystems) into the cache.

Great for dev, tests, and self-hosters who already have a media library on disk.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)

AUDIO_EXTS = frozenset({".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac"})


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
            if path.suffix.lower() not in AUDIO_EXTS:
                continue
            rel = path.relative_to(self.media_root)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            tracks.append(
                ProviderTrack(
                    title=path.stem,
                    source_ref=str(rel),
                    size_bytes=size,
                )
            )
        return tracks

    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        src = self.media_root / source_ref
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
