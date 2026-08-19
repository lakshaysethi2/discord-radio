"""Core file-provider logic: playlist cursor + fetch orchestration.

The FastAPI app (``api/main.py``) is a thin HTTP shim over this.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import re
import sqlite3
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


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Pure month-year names: the whole title is e.g. 'apr-2002' (gdrive monthly
# lecture files). Not '...TheWaytoGod-February2002[B00..]' — that's an
# audiobook title and must NOT claim the Feb-2002 lecture slot.
_PURE_MONTH_YEAR_RE = re.compile(
    r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[- _]?(\d{2}|\d{4})$"
)
# BTO-style archive titles: '#NN - MM_DD_YY - #HASH'.
_BTO_DATE_RE = re.compile(r"#\d+[- _]+(\d{1,2})[-_](\d{1,2})[-_](\d{2})")


def _date_key(title: str) -> str | None:
    """Extract a 'YYYY-MM' key from a title, only when unambiguous.

    Two shapes qualify:
      * the whole title is a month-year name ('apr-2002', 'nov 2001');
      * the title contains a BTO-style token '#NN - MM_DD_YY' (archive side).

    Everything else (audiobook titles with an embedded month, satsang
    compilations, ...) gets no date key — a wrong date match would silently
    swap a lecture for an audiobook in the playlist.
    """
    t = title.strip().lower()
    m = _PURE_MONTH_YEAR_RE.match(t)
    if m:
        year = int(m.group(2))
        if year < 100:
            year += 2000
        if not 1990 <= year <= 2100:
            return None
        return f"{year:04d}-{_MONTHS[m.group(1)]:02d}"
    m = _BTO_DATE_RE.search(t)
    if m:
        month, _day, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= month <= 12 or not 1 <= _day <= 31:
            return None
        return f"{2000 + yy:04d}-{month:02d}"
    return None


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
    def refresh_playlist(
        self,
        archive_org_items: str | list[str] | None = None,
        gdrive_webdav_url: str | None = None,
        gdrive_webdav_path: str | None = None,
    ) -> dict:
        """Ask every configured provider for its tracks; merge into the DB.

        ``gdrive_webdav_url``/``gdrive_webdav_path`` let the dashboard repoint
        the webdav provider at runtime (same pattern as archive_org_items).
        """
        # Only touch archive.org when the caller explicitly passed item ids
        # (dashboard form). Never pull ARCHIVE_ORG_ITEMS out of tv.db on a
        # Drive rescan — that used to inject archive mid-refresh, prune an
        # empty webdav result, and leave the radio on archive.org 503s.
        if archive_org_items is not None:
            if isinstance(archive_org_items, str):
                items = [x.strip() for x in archive_org_items.split(",") if x.strip()]
            else:
                items = [x.strip() for x in archive_org_items if x.strip()]

            from file_provider.providers.archive import ArchiveOrgProvider

            with self._lock:
                archive_provider = next(
                    (p for p in self.providers if isinstance(p, ArchiveOrgProvider)), None
                )
                if archive_provider is None:
                    if items:
                        archive_provider = ArchiveOrgProvider(item_ids=items)
                        self.providers.append(archive_provider)
                        self._provider_by_name[archive_provider.name] = archive_provider
                else:
                    archive_provider.item_ids = items

        if gdrive_webdav_url is not None or gdrive_webdav_path is not None:
            from file_provider.providers.webdav import WebDavProvider

            with self._lock:
                webdav_provider = next(
                    (p for p in self.providers if isinstance(p, WebDavProvider)), None
                )
                if webdav_provider is None:
                    webdav_provider = WebDavProvider(
                        url=gdrive_webdav_url or "",
                        path=gdrive_webdav_path or "/",
                    )
                    self.providers.append(webdav_provider)
                    self._provider_by_name[webdav_provider.name] = webdav_provider
                else:
                    if gdrive_webdav_url is not None:
                        webdav_provider.url = (gdrive_webdav_url or "").rstrip("/")
                    if gdrive_webdav_path is not None:
                        webdav_provider.path = posixpath.normpath(
                            "/" + (gdrive_webdav_path or "/").lstrip("/")
                        )
                        if webdav_provider.path == "/.":
                            webdav_provider.path = "/"

        added_total = updated_total = 0
        errors: dict[str, str] = {}
        for provider in self.providers:
            if not provider.is_configured():
                # Keep existing rows. An empty URL mid-outage must not wipe
                # the Drive playlist the radio is currently playing.
                log.info("skip provider %s: not configured", provider.name)
                continue
            try:
                found = provider.list_tracks()
            except Exception as exc:
                log.exception("provider %s scan failed", provider.name)
                self.db.mark_provider(provider.name, healthy=False, error=str(exc))
                errors[provider.name] = str(exc)
                continue
            if not found:
                # list_tracks() returning [] used to prune every row for this
                # provider and left the radio on archive.org 503s. Keep the
                # last good playlist until a scan actually finds files.
                log.warning(
                    "provider %s scan returned 0 tracks — keeping existing rows",
                    provider.name,
                )
                continue
            self.db.mark_provider(provider.name, healthy=True)
            rows = self._provider_tracks_to_rows(provider, found)
            added, updated = self.db.upsert_tracks(rows)
            active_refs = {t.source_ref for t in found}
            self.db.prune_provider_tracks(provider.name, active_refs)
            added_total += added
            updated_total += updated

        # Drop rows whose provider is no longer active (e.g. stale 'local'
        # rows from a FUSE-mounted era). Ghost rows would 502 mid-playlist.
        active = {p.name for p in self.providers}
        with self._lock:
            for row in self.db.fetchall("SELECT DISTINCT provider FROM tracks"):
                name = row["provider"]
                if name not in active:
                    pruned = self.db.prune_provider_tracks(name, set())
                    if pruned:
                        log.info("pruned %d stale tracks from inactive provider %s", pruned, name)

        # Provider migration: when webdav (gdrive) is present alongside archive,
        # re-point title/date-matched archive rows onto their gdrive counterparts
        # so the playlist keeps serving the same content from the primary source.
        names = {p.name for p in self.providers}
        if "webdav" in names and "archive" in names:
            moved = self.migrate_provider(primary="webdav", fallback="archive")
            if moved:
                log.info("re-pointed %d archive tracks onto webdav", moved)

        return {
            "added": added_total,
            "updated": updated_total,
            "total": self.db.playlist_length(),
            "errors": errors,
        }

    # ------------------------------------------------------------ migration
    def migrate_provider(self, primary: str, fallback: str) -> int:
        """Move `fallback` rows onto their title-matched `primary` counterparts.

        Matched primary rows adopt the fallback row's playlist position; the
        fallback row is dropped, and unmatched primary rows are appended after
        the remaining fallback rows. The result is renumbered sequentially, so
        the playlist cursor keeps pointing at the same content it did before
        (now served from the primary source). Idempotent across refreshes.
        """
        with self._lock:
            primary_rows = self.db.fetchall(
                "SELECT * FROM tracks WHERE provider=? ORDER BY sort_order, track_id",
                (primary,),
            )
            fallback_rows = self.db.fetchall(
                "SELECT * FROM tracks WHERE provider=? ORDER BY sort_order, track_id",
                (fallback,),
            )
            if not fallback_rows or not primary_rows:
                return 0

            used: set[str] = set()
            partner_of: dict[str, str] = {}  # fallback track_id -> primary track_id
            for fb in fallback_rows:
                fb_keys = self._match_keys(fb["title"])
                if not fb_keys:
                    continue
                for pr in primary_rows:
                    if pr["track_id"] in used:
                        continue
                    if fb_keys & self._match_keys(pr["title"]):
                        used.add(pr["track_id"])
                        partner_of[fb["track_id"]] = pr["track_id"]
                        break

            if not partner_of:
                return 0

            # Final order: fallback rows (with matched ones replaced by their
            # primary partner at the same position), then unmatched primaries.
            final_order: list[str] = []
            for fb in fallback_rows:
                final_order.append(partner_of.get(fb["track_id"], fb["track_id"]))
            for pr in primary_rows:
                if pr["track_id"] not in used:
                    final_order.append(pr["track_id"])

            return self.db.repoint_tracks(drop_ids=set(partner_of), final_order=final_order)

    @staticmethod
    def _match_keys(title: str) -> set[str]:
        """Keys for cross-provider title matching.

        Always the alnum-normalized title; plus a 'YYYY-MM' date key when the
        title contains a parseable month/year (e.g. '#03 - 04_25_02 - #9B58'
        ↔ 'apr-2002').
        """
        keys: set[str] = set()
        norm = re.sub(r"[^a-z0-9]", "", title.lower())
        if norm:
            keys.add(norm)
        date_key = _date_key(title.lower())
        if date_key:
            keys.add(date_key)
        return keys

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
            n = self.db.playlist_length()
            if n == 0:
                raise PlaylistEmpty("playlist is empty")
            last_exc: Exception | None = None
            for _ in range(n):
                row = self._next_playable_row()
                if row is None:
                    break
                try:
                    payload = self._ensure_and_wrap(row)
                    self._kick_prefetch()
                    return payload
                except ProviderFetchError as exc:
                    last_exc = exc
                    log.warning("skip %s: fetch failed: %s", row["track_id"], exc)
                    self.db.advance_cursor(1)
            if last_exc is not None:
                raise last_exc
            raise PlaylistEmpty("playlist is empty")

    def next(self) -> TrackPayload:
        with self._lock:
            self.db.advance_cursor(1)
            return self.current()

    def _next_playable_row(self) -> sqlite3.Row | None:
        """Row at the cursor, skipping tracks whose provider is unhealthy.

        Skips at most one full lap of the playlist so a mostly-down provider
        doesn't make the radio loop forever; if every row's provider is
        unhealthy the cursor row itself is returned and the fetch attempt
        surfaces the failure.
        """
        n = self.db.playlist_length()
        if n == 0:
            return None
        for _ in range(n):
            row = self.db.track_at(self.db.get_cursor())
            if row is None:
                return None
            if self.db.provider_healthy(row["provider"]):
                return row
            log.info(
                "skip %s: provider %s unhealthy", row["track_id"], row["provider"]
            )
            self.db.advance_cursor(1)
        return self.db.track_at(self.db.get_cursor())

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
        providers = _providers_from_config(config)
    return Service(db=db, cache=cache, providers=providers)


def _providers_from_config(config: Config) -> list[BaseProvider]:
    """Instantiate providers per FILE_PROVIDER_ORDER."""
    from file_provider.providers.local import LocalProvider

    archive_items = list(config.archive_org_items)

    out: list[BaseProvider] = []
    for name in config.provider_order:
        if name == "local":
            out.append(LocalProvider(config.local_media_path))
        elif name == "archive":
            from file_provider.providers.archive import ArchiveOrgProvider

            out.append(ArchiveOrgProvider(item_ids=archive_items))
        elif name == "webdav":
            from file_provider.providers.webdav import WebDavProvider

            out.append(
                WebDavProvider(
                    url=config.gdrive_webdav_url,
                    path=config.gdrive_webdav_path,
                    username=config.gdrive_webdav_user,
                    password=config.gdrive_webdav_pass,
                )
            )
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

    # Do not auto-append archive.org. Drive (webdav) is the radio source;
    # archive only loads when it is listed in FILE_PROVIDER_ORDER.
    return out
