"""LRU cache management.

The cache is a plain directory of files named ``<track_id>.<ext>``. We track
metadata in the DB (``cache_entries``); on-disk state is the source of truth
for actual bytes.

When adding a new file would exceed ``max_bytes``, we evict the least-recently-
touched entries (via ``cache_lru()``) until enough room is free. We never
evict the currently-playing track — the caller passes it in as ``protect``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from file_provider.db import ProviderDB
from file_provider.providers.base import ProviderFetchError
from file_provider.storage import StorageQuota

log = logging.getLogger(__name__)


class StorageQuotaExceeded(ProviderFetchError):
    """The provider-managed disk budget cannot fit another media file."""


class Cache:
    def __init__(
        self,
        root: Path,
        db: ProviderDB,
        max_bytes: int,
        *,
        quota: StorageQuota | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.max_bytes = max_bytes
        self.quota = quota

    # ------------------------------------------------------------- lookup
    def path_for(self, track_id: str, ext: str = ".audio") -> Path:
        # ``.audio`` is a benign fallback — the real extension gets set by
        # the caller when it knows the source's mime/format. FFmpeg is content-
        # sniffing anyway.
        return self.root / f"{track_id}{ext}"

    def get(self, track_id: str) -> Path | None:
        row = self.db.cache_entry(track_id)
        if row is None:
            return None
        p = Path(row["file_path"])
        if not p.exists():
            # Disk out-of-sync with DB — clean up.
            self.db.forget_cache(track_id)
            return None
        self.db.touch_cache(track_id)
        return p

    # -------------------------------------------------------------- write
    def record(self, track_id: str, path: Path) -> None:
        size = path.stat().st_size if path.exists() else 0
        self.db.record_cache(track_id, str(path), size)

    def evict_until_free(self, needed_bytes: int, protect: set[str] | None = None) -> int:
        """Evict LRU entries until we have room for `needed_bytes`.

        Returns bytes freed. `protect` is a set of track_ids we must not evict
        (currently-playing + next-up).
        """
        protect = protect or set()
        current = self.db.cache_total_bytes()
        if self.quota is not None:
            if self.quota.allows(needed_bytes):
                return 0
        elif current + needed_bytes <= self.max_bytes:
            return 0

        freed = 0
        for row in self.db.cache_lru():
            if self.quota is not None:
                if self.quota.allows(needed_bytes):
                    break
            elif current + needed_bytes - freed <= self.max_bytes:
                break
            if row["track_id"] in protect:
                continue
            p = Path(row["file_path"])
            size = int(row["size_bytes"])
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("could not delete %s: %s", p, exc)
                continue
            self.db.forget_cache(row["track_id"])
            freed += size
            log.info("evicted %s (%d bytes)", row["track_id"], size)
        if self.quota is not None and not self.quota.allows(needed_bytes):
            raise StorageQuotaExceeded(
                "provider storage quota exceeded; remove torrent data or increase "
                "FILE_PROVIDER_CACHE_MAX_GB"
            )
        return freed

    def prune_orphans(self) -> int:
        """Remove DB rows whose files vanished; return count removed."""
        removed = 0
        for row in self.db.cache_lru():
            p = Path(row["file_path"])
            if not p.exists():
                self.db.forget_cache(row["track_id"])
                removed += 1
        return removed

    def total_bytes(self) -> int:
        return self.db.cache_total_bytes()

    def free_bytes(self) -> int:
        if self.quota is not None:
            return self.quota.free_bytes()
        return max(0, self.max_bytes - self.total_bytes())

    def storage_total_bytes(self) -> int:
        """Actual provider-managed disk usage, counting hardlinks once."""
        return self.quota.usage_bytes() if self.quota is not None else self.total_bytes()

    def rebuild_from_disk(self) -> int:
        """Rescan the cache dir and re-register orphan files.

        Useful after a crash / manual copy. Returns count re-added.
        """
        known: set[str] = {r["file_path"] for r in self.db.cache_lru()}
        added = 0
        for path in self.root.iterdir():
            if not path.is_file():
                continue
            if str(path) in known:
                continue
            # Filename shape: <track_id><ext> — track_id has no dots so split on last dot.
            track_id = path.stem
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self.db.record_cache(track_id, str(path), size)
            added += 1
        return added

    def clear_all(self) -> None:
        for row in self.db.cache_lru():
            p = Path(row["file_path"])
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            self.db.forget_cache(row["track_id"])

    # ---------------------------------------------------- fs sanity helpers
    def disk_free_bytes(self) -> int:  # pragma: no cover — depends on FS
        try:
            st = os.statvfs(self.root)
            return st.f_bavail * st.f_frsize
        except OSError:
            return 0
