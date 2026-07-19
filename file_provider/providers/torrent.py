"""Torrent-backed playlist provider.

``TorrentManager`` handles the aria2 lifecycle and dashboard operations. This
provider adapts administrator-enabled torrent files to the normal file-provider
contract so the existing cache, prefetcher and Discord bot can play them with
no torrent-specific code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from file_provider.media_types import is_video_ext
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack
from file_provider.torrent_client import TorrentManager, is_metadata_path

log = logging.getLogger(__name__)


class TorrentProvider(BaseProvider):
    name = "torrent"

    def __init__(self, manager: TorrentManager, *, allowed_extensions=None) -> None:
        self.manager = manager
        self.allowed_extensions = (
            manager.allowed_extensions if allowed_extensions is None else allowed_extensions
        )

    def start(self) -> bool:
        ready = self.manager.start()
        if ready:
            # Refresh persisted file completion before the API decides whether
            # an initial playlist scan is necessary.
            try:
                self.manager.list_torrents()
            except Exception as exc:
                log.debug("initial torrent status refresh failed: %s", exc)
        return ready

    def stop(self) -> None:
        self.manager.stop()

    def is_configured(self) -> bool:
        # A missing aria2 binary should not make local/archive playback fail.
        return self.manager.available

    def list_tracks(self) -> list[ProviderTrack]:
        if not self.manager.available and not self.manager.start():
            log.warning("torrent provider unavailable: %s", self.manager.last_start_error)
            return []
        # Refresh progress/completion before exposing files to the normal
        # playlist. A long-running torrent can finish between dashboard
        # requests; the bot should see that transition on its next rescan.
        try:
            self.manager.list_torrents()
        except Exception as exc:
            log.debug("torrent status refresh failed: %s", exc)
        tracks: list[ProviderTrack] = []
        for file in self.manager.db.selected_torrent_files():
            path = Path(file["path"])
            if is_metadata_path(str(path)):
                continue
            if (
                path.suffix.lower() not in self.allowed_extensions
                and not bool(file["media_override"])
            ):
                continue
            leaf = path.name or str(path)
            torrent_name = file["torrent_name"] or "Torrent"
            source_ref = self.manager.source_ref(file["gid"], file["file_index"])
            tracks.append(
                ProviderTrack(
                    title=f"{torrent_name} — {leaf}",
                    source_ref=source_ref,
                    size_bytes=int(file["length"] or 0),
                    has_video=is_video_ext(path.suffix),
                )
            )
        return tracks

    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        try:
            return self.manager.ensure_cached(source_ref, target_path)
        except ProviderFetchError:
            raise
        except Exception as exc:  # defensive boundary for the Service
            raise ProviderFetchError(f"torrent fetch failed: {exc}") from exc
