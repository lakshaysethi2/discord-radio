"""Core file-provider logic: playlist cursor + fetch orchestration.

The FastAPI app (``api/main.py``) is a thin HTTP shim over this.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from file_provider.cache import Cache
from file_provider.db import ProviderDB
from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

if TYPE_CHECKING:  # pragma: no cover
    from file_provider.config import Config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TrackPayload:
    """Matches the bot-facing JSON contract (blueprint §4.1)."""

    track_id: str
    title: str
    duration_seconds: int
    local_path: str
    provider_used: str
    playlist_position: int
    ready: bool
    has_video: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class PlaylistEmpty(RuntimeError):
    pass


class Service:
    """Orchestrates providers, DB and cache into a coherent playlist.

    Thread-safe: an internal lock serializes cursor advances and prefetches.
    """

    def __init__(
        self,
        db: ProviderDB,
        cache: Cache,
        providers: list[BaseProvider],
    ) -> None:
        self.db = db
        self.cache = cache
        # Providers are ordered by preference; the first one that has a track
        # for a source_ref wins. In practice each track belongs to exactly
        # one provider (we store that on the row).
        self.providers = providers
        self._provider_by_name = {p.name: p for p in providers}
        self._lock = threading.RLock()
        self._prefetch_thread: threading.Thread | None = None
        # Per-track fetch locks so foreground and prefetch never race on the
        # same file. Access to _fetch_locks itself is guarded by _lock.
        self._fetch_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start lifecycle-aware providers such as the local aria2 client."""
        for provider in self.providers:
            start = getattr(provider, "start", None)
            if not callable(start):
                continue
            try:
                start()
            except Exception as exc:  # one optional backend must not kill the service
                log.warning("provider %s failed to start: %s", provider.name, exc)

    def shutdown(self) -> None:
        """Stop optional provider processes during ASGI shutdown."""
        for provider in self.providers:
            stop = getattr(provider, "stop", None)
            if not callable(stop):
                continue
            with contextlib.suppress(Exception):
                stop()

    def torrent_provider(self):
        """Return the configured torrent provider, if present."""
        for provider in self.providers:
            if provider.name == "torrent":
                return provider
        return None

    def _fetch_lock(self, track_id: str) -> threading.Lock:
        with self._lock:
            lk = self._fetch_locks.get(track_id)
            if lk is None:
                lk = threading.Lock()
                self._fetch_locks[track_id] = lk
            return lk

    # ---------------------------------------------------------------- scan
    def refresh_playlist(self) -> dict:
        """Ask every configured provider for its tracks; merge into the DB."""
        added_total = updated_total = 0
        errors: dict[str, str] = {}
        for provider in self.providers:
            if not provider.is_configured():
                log.info("skip provider %s: not configured", provider.name)
                continue
            try:
                found = provider.list_tracks()
            except Exception as exc:
                log.exception("provider %s scan failed", provider.name)
                self.db.mark_provider(provider.name, healthy=False, error=str(exc))
                errors[provider.name] = str(exc)
                continue
            self.db.mark_provider(provider.name, healthy=True)
            rows = self._provider_tracks_to_rows(provider, found)
            added, updated = self.db.upsert_tracks(rows)
            # A successful scan is authoritative. In particular this removes
            # torrent files that an admin disabled from the playable playlist;
            # older backends also benefit when a source file is deleted.
            removed = self.db.remove_provider_tracks_not_in(
                provider.name, {row["source_ref"] for row in rows}
            )
            for _track_id, cache_path in removed:
                if cache_path:
                    with contextlib.suppress(OSError):
                        Path(cache_path).unlink()
            added_total += added
            updated_total += updated
        return {
            "added": added_total,
            "updated": updated_total,
            "total": self.db.playlist_length(),
            "errors": errors,
        }

    @staticmethod
    def _provider_tracks_to_rows(provider: BaseProvider, tracks: list[ProviderTrack]) -> list[dict]:
        rows: list[dict] = []
        for idx, t in enumerate(tracks):
            rows.append(
                {
                    "track_id": t.track_id(provider.name),
                    "title": t.title,
                    "duration_seconds": t.duration_seconds,
                    "size_bytes": t.size_bytes,
                    "provider": provider.name,
                    "source_ref": t.source_ref,
                    "sort_order": float(idx),
                    "has_video": t.has_video,
                }
            )
        return rows

    # ----------------------------------------------------------- accessors
    def current(self) -> TrackPayload:
        with self._lock:
            row = self.db.track_at(self.db.get_cursor())
            if row is None:
                raise PlaylistEmpty("playlist is empty")
            payload = self._ensure_and_wrap(row)
            self._kick_prefetch()
            return payload

    def next(self) -> TrackPayload:
        with self._lock:
            self.db.advance_cursor(1)
            return self.current()

    def peek(self, count: int) -> list[TrackPayload]:
        with self._lock:
            rows = self.db.peek(self.db.get_cursor(), count)
            # Peek doesn't force downloads — it just returns metadata.
            out: list[TrackPayload] = []
            cur = self.db.get_cursor()
            for i, row in enumerate(rows):
                cached = self.cache.get(row["track_id"])
                out.append(
                    TrackPayload(
                        track_id=row["track_id"],
                        title=row["title"],
                        duration_seconds=int(row["duration_seconds"] or 0),
                        local_path=str(cached) if cached else "",
                        provider_used=row["provider"],
                        playlist_position=(cur + i) % max(1, self.db.playlist_length()),
                        ready=cached is not None,
                        # sqlite3.Row's __contains__ checks values, not keys —
                        # .keys() is required. (SIM118 doesn't understand this.)
                        has_video=bool(row["has_video"])
                        if "has_video" in row.keys()  # noqa: SIM118
                        else False,
                    )
                )
            return out

    def get_by_id(self, track_id: str) -> TrackPayload:
        with self._lock:
            row = self.db.fetchone("SELECT * FROM tracks WHERE track_id=?", (track_id,))
            if row is None:
                raise KeyError(track_id)
            return self._ensure_and_wrap(row)

    def list_all(
        self, *, offset: int = 0, limit: int = 100, search: str | None = None
    ) -> tuple[list[TrackPayload], int]:
        """Return a metadata-only track page and total; never downloads files."""
        with self._lock:
            rows = self.db.list_all(offset=offset, limit=limit, search=search)
            total = self.db.count_tracks(search=search)
            payloads = [self._metadata_payload(row) for row in rows]
            return payloads, total

    def jump_to(self, track_id: str) -> TrackPayload:
        """Set the playlist cursor and fetch the selected track for immediate play."""
        with self._lock:
            position = self.db.position_of(track_id)
            if position is None:
                raise KeyError(track_id)
            self.db.set_cursor(position)
            return self.current()

    def current_track_id(self) -> str | None:
        with self._lock:
            row = self.db.track_at(self.db.get_cursor())
            return row["track_id"] if row else None

    def _metadata_payload(self, row) -> TrackPayload:
        cache_path = row["cache_file_path"]
        cached = Path(cache_path) if cache_path and Path(cache_path).is_file() else None
        return TrackPayload(
            track_id=row["track_id"],
            title=row["title"],
            duration_seconds=int(row["duration_seconds"] or 0),
            local_path=str(cached) if cached else "",
            provider_used=row["provider"],
            playlist_position=int(row["playlist_position"]),
            ready=cached is not None,
            has_video=bool(row["has_video"]),
        )

    def mark_played(self, track_id: str) -> None:
        # Refresh LRU timestamp so the just-played file doesn't get evicted
        # first if it appears again soon.
        self.db.touch_cache(track_id)

    # --------------------------------------------------------------- inner
    def _ensure_and_wrap(self, row) -> TrackPayload:
        """Fetch if needed and build the JSON payload.

        Holds a per-track fetch lock while downloading so foreground callers
        and the prefetch thread never race on the same file. The DB lookup
        happens twice around the lock (double-check) so we don't block just
        to re-report a fully-cached file.
        """
        provider = self._provider_by_name.get(row["provider"])
        if provider is None:
            raise ProviderFetchError(f"unknown provider '{row['provider']}'")

        target = self.cache.path_for(row["track_id"])
        cached = self.cache.get(row["track_id"])
        if cached is not None:
            local_path = cached
            ready = True
        else:
            with self._fetch_lock(row["track_id"]):
                # Someone else may have populated it while we waited.
                cached = self.cache.get(row["track_id"])
                if cached is not None:
                    local_path = cached
                else:
                    needed = int(row["size_bytes"] or 0) or 50 * 1024 * 1024
                    protect = {row["track_id"]}
                    self.cache.evict_until_free(needed, protect=protect)
                    try:
                        local_path = provider.ensure_cached(row["source_ref"], target)
                        self.db.mark_provider(provider.name, healthy=True)
                    except ProviderFetchError:
                        self.db.mark_provider(provider.name, healthy=False, error="fetch failed")
                        raise
                    self.cache.record(row["track_id"], local_path)
            ready = True

        return TrackPayload(
            track_id=row["track_id"],
            title=row["title"],
            duration_seconds=int(row["duration_seconds"] or 0),
            local_path=str(local_path),
            provider_used=provider.name,
            playlist_position=self._position_of(row["track_id"]),
            ready=ready,
            has_video=bool(row["has_video"])
            if "has_video" in row.keys()  # noqa: SIM118  (sqlite3.Row.__contains__ checks values)
            else False,
        )

    def _position_of(self, track_id: str) -> int:
        """Return the 0-indexed position of `track_id` in the sorted playlist."""
        return self.db.position_of(track_id) or 0

    # -------------------------------------------------------- pre-fetch bg
    def _kick_prefetch(self) -> None:
        """Fire-and-forget background download of the next track."""
        n = self.db.playlist_length()
        if n < 2:
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return

        next_row = self.db.track_at(self.db.get_cursor() + 1)
        if next_row is None:
            return
        if self.cache.get(next_row["track_id"]) is not None:
            return

        def _do_prefetch(row):
            provider = self._provider_by_name.get(row["provider"])
            if provider is None:
                return
            target = self.cache.path_for(row["track_id"])
            try:
                if self.db.closed:
                    return
                with self._fetch_lock(row["track_id"]):
                    # If someone else cached it first, skip.
                    if self.cache.get(row["track_id"]) is not None:
                        return
                    needed = int(row["size_bytes"] or 0) or 50 * 1024 * 1024
                    self.cache.evict_until_free(needed, protect=self._current_and_next())
                    provider.ensure_cached(row["source_ref"], target)
                    if self.db.closed:
                        return
                    self.cache.record(row["track_id"], target)
                self.db.mark_provider(provider.name, healthy=True)
                log.info("prefetched %s", row["track_id"])
            except Exception as exc:
                log.warning("prefetch %s failed: %s", row["track_id"], exc)
                if not self.db.closed:
                    with contextlib.suppress(Exception):  # pragma: no cover — best-effort
                        self.db.mark_provider(provider.name, healthy=False, error=str(exc))

        self._prefetch_thread = threading.Thread(
            target=_do_prefetch, args=(next_row,), daemon=True, name="prefetch"
        )
        self._prefetch_thread.start()

    def _current_and_next(self) -> set[str]:
        n = self.db.playlist_length()
        if n == 0:
            return set()
        cur = self.db.track_at(self.db.get_cursor())
        nxt = self.db.track_at(self.db.get_cursor() + 1) if n > 1 else None
        out: set[str] = set()
        if cur:
            out.add(cur["track_id"])
        if nxt:
            out.add(nxt["track_id"])
        return out


def build_service(config: Config, providers: list[BaseProvider] | None = None) -> Service:
    """Wire everything together from a Config."""
    db = ProviderDB(config.db_path)
    cache = Cache(Path(config.cache_path), db, config.cache_max_bytes)
    if providers is None:
        providers = _providers_from_config(config, db)
    return Service(db=db, cache=cache, providers=providers)


def _providers_from_config(config: Config, db: ProviderDB | None = None) -> list[BaseProvider]:
    """Instantiate providers per FILE_PROVIDER_ORDER."""
    from file_provider.providers.local import LocalProvider

    out: list[BaseProvider] = []
    provider_names = list(config.provider_order)
    # Keep the management API useful for existing installations whose .env
    # predates the torrent backend and still says FILE_PROVIDER_ORDER=local.
    if config.torrent_enabled and "torrent" not in provider_names:
        provider_names.append("torrent")
    for name in provider_names:
        if name == "local":
            out.append(LocalProvider(config.local_media_path))
        elif name == "torrent":
            if not config.torrent_enabled:
                log.info("torrent provider disabled by configuration")
                continue
            from file_provider.providers.torrent import TorrentProvider
            from file_provider.torrent_client import TorrentManager

            out.append(
                TorrentProvider(
                    TorrentManager(
                        db or ProviderDB(config.db_path),
                        config.torrent_data_path,
                        rpc_url=config.torrent_rpc_url,
                        rpc_secret=config.torrent_rpc_secret,
                        rpc_port=config.torrent_rpc_port,
                        binary=config.torrent_binary,
                        allow_remote_rpc=config.torrent_allow_remote_rpc,
                        max_size_bytes=config.torrent_max_size_bytes,
                        max_upload_bytes=config.torrent_max_upload_bytes,
                        allowed_extensions=config.torrent_allowed_extensions,
                    )
                )
            )
        elif name == "archive":
            from file_provider.providers.archive import ArchiveOrgProvider

            out.append(ArchiveOrgProvider(item_ids=config.archive_org_items))
        elif name == "telegram":
            from file_provider.providers.telegram import TelegramProvider

            out.append(
                TelegramProvider(
                    api_id=config.telegram_api_id,
                    api_hash=config.telegram_api_hash,
                    channel_id=config.telegram_channel_id,
                    session_path=config.telethon_session_path(),
                )
            )
        else:
            log.warning("unknown provider '%s' in FILE_PROVIDER_ORDER", name)
    return out
