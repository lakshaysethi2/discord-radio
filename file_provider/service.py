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
                    )
                )
            return out

    def get_by_id(self, track_id: str) -> TrackPayload:
        with self._lock:
            row = self.db.fetchone("SELECT * FROM tracks WHERE track_id=?", (track_id,))
            if row is None:
                raise KeyError(track_id)
            return self._ensure_and_wrap(row)

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
        )

    def _position_of(self, track_id: str) -> int:
        """Return the 0-indexed position of `track_id` in the sorted playlist."""
        row = self.db.fetchone(
            "SELECT sort_order FROM tracks WHERE track_id=?",
            (track_id,),
        )
        if row is None:
            return 0
        n_row = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM tracks "
            "WHERE sort_order < ? OR (sort_order = ? AND track_id < ?)",
            (row["sort_order"], row["sort_order"], track_id),
        )
        return int(n_row["n"]) if n_row else 0

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
        providers = _providers_from_config(config)
    return Service(db=db, cache=cache, providers=providers)


def _providers_from_config(config: Config) -> list[BaseProvider]:
    """Instantiate providers per FILE_PROVIDER_ORDER."""
    from file_provider.providers.local import LocalProvider

    out: list[BaseProvider] = []
    for name in config.provider_order:
        if name == "local":
            out.append(LocalProvider(config.local_media_path))
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
