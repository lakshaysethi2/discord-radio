"""SQLite persistence for the file-provider service.

Owns:
* the full playlist (tracks + sort order)
* current playlist cursor
* per-provider health flags
* per-track cache metadata (last accessed timestamp for LRU eviction)

This is separate from the bot's DB — the bot only holds session data.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracks (
        track_id          TEXT PRIMARY KEY,
        title             TEXT NOT NULL,
        duration_seconds  INTEGER DEFAULT 0,
        size_bytes        INTEGER DEFAULT 0,
        provider          TEXT NOT NULL,     -- provider name that owns the source
        source_ref        TEXT NOT NULL,     -- provider-specific pointer (e.g. telegram msg id)
        sort_order        REAL NOT NULL,     -- REAL so we can insert in-between
        added_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(provider, source_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tracks_sort ON tracks(sort_order)",
    # No FK to tracks: rebuild_from_disk() and manual copy-in scenarios need
    # to record files whose track_id may not (yet) exist in `tracks`.
    """
    CREATE TABLE IF NOT EXISTS cache_entries (
        track_id       TEXT PRIMARY KEY,
        file_path      TEXT NOT NULL,
        size_bytes     INTEGER NOT NULL,
        last_accessed  DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cache_lru ON cache_entries(last_accessed)",
    """
    CREATE TABLE IF NOT EXISTS provider_health (
        provider     TEXT PRIMARY KEY,
        healthy      BOOLEAN DEFAULT 1,
        last_success DATETIME,
        last_failure DATETIME,
        last_error   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
)

# Well-known state keys.
STATE_CURSOR = "playlist_cursor"


class ProviderDB:
    """Small sqlite3 wrapper — shares the shape of the bot's `Database`."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._configure()
            self.migrate()
        except Exception:
            self._conn.close()
            raise

    def _configure(self) -> None:
        cur = self._conn.cursor()
        try:
            if self.path != ":memory:":
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()

    def migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                for stmt in SCHEMA:
                    cur.execute(stmt)
            finally:
                cur.close()

    # -------------------------------------------------------------- helpers
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[tuple | dict]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, seq)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._conn.close()

    @property
    def closed(self) -> bool:
        return getattr(self, "_closed", False)

    def __enter__(self) -> ProviderDB:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- state kv
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.fetchone("SELECT value FROM state WHERE key=?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str | int | float | bool) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------ playlist
    def playlist_length(self) -> int:
        row = self.fetchone("SELECT COUNT(*) AS n FROM tracks")
        return int(row["n"]) if row else 0

    def get_cursor(self) -> int:
        v = self.get_state(STATE_CURSOR, "0")
        try:
            return int(v) if v is not None else 0
        except ValueError:
            return 0

    def set_cursor(self, position: int) -> None:
        n = self.playlist_length()
        if n == 0:
            self.set_state(STATE_CURSOR, 0)
            return
        self.set_state(STATE_CURSOR, position % n)

    def advance_cursor(self, by: int = 1) -> int:
        n = self.playlist_length()
        if n == 0:
            return 0
        new = (self.get_cursor() + by) % n
        self.set_state(STATE_CURSOR, new)
        return new

    def track_at(self, position: int) -> sqlite3.Row | None:
        # Positions are 0-indexed against the sort_order-ordered list.
        n = self.playlist_length()
        if n == 0:
            return None
        pos = position % n
        return self.fetchone(
            "SELECT * FROM tracks ORDER BY sort_order, track_id LIMIT 1 OFFSET ?",
            (pos,),
        )

    def peek(self, start: int, count: int) -> list[sqlite3.Row]:
        n = self.playlist_length()
        if n == 0 or count <= 0:
            return []
        rows: list[sqlite3.Row] = []
        # Simple wrap-around: fetch [start:] and [:remainder] if needed.
        first = self.fetchall(
            "SELECT * FROM tracks ORDER BY sort_order, track_id LIMIT ? OFFSET ?",
            (min(count, n - (start % n)), start % n),
        )
        rows.extend(first)
        remaining = count - len(rows)
        if remaining > 0:
            rows.extend(
                self.fetchall(
                    "SELECT * FROM tracks ORDER BY sort_order, track_id LIMIT ?",
                    (remaining,),
                )
            )
        return rows

    def upsert_tracks(self, tracks: list[dict]) -> tuple[int, int]:
        """Insert new tracks, update existing. Returns (added, updated)."""
        added = updated = 0
        with self.transaction() as cur:
            for t in tracks:
                cur.execute(
                    "SELECT track_id FROM tracks WHERE provider=? AND source_ref=?",
                    (t["provider"], t["source_ref"]),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO tracks(track_id, title, duration_seconds, size_bytes, "
                        "provider, source_ref, sort_order) VALUES(?,?,?,?,?,?,?)",
                        (
                            t["track_id"],
                            t["title"],
                            int(t.get("duration_seconds") or 0),
                            int(t.get("size_bytes") or 0),
                            t["provider"],
                            t["source_ref"],
                            float(t["sort_order"]),
                        ),
                    )
                    added += 1
                else:
                    cur.execute(
                        "UPDATE tracks SET title=?, duration_seconds=?, size_bytes=?, sort_order=? "
                        "WHERE provider=? AND source_ref=?",
                        (
                            t["title"],
                            int(t.get("duration_seconds") or 0),
                            int(t.get("size_bytes") or 0),
                            float(t["sort_order"]),
                            t["provider"],
                            t["source_ref"],
                        ),
                    )
                    updated += 1
        return added, updated

    # -------------------------------------------------------------- cache
    def record_cache(self, track_id: str, path: str, size_bytes: int) -> None:
        self.execute(
            "INSERT INTO cache_entries(track_id, file_path, size_bytes) VALUES(?,?,?) "
            "ON CONFLICT(track_id) DO UPDATE SET file_path=excluded.file_path, "
            "size_bytes=excluded.size_bytes, last_accessed=CURRENT_TIMESTAMP",
            (track_id, path, size_bytes),
        )

    def touch_cache(self, track_id: str) -> None:
        self.execute(
            "UPDATE cache_entries SET last_accessed=CURRENT_TIMESTAMP WHERE track_id=?",
            (track_id,),
        )

    def cache_entry(self, track_id: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM cache_entries WHERE track_id=?", (track_id,))

    def cache_lru(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM cache_entries ORDER BY last_accessed ASC")

    def cache_total_bytes(self) -> int:
        row = self.fetchone("SELECT COALESCE(SUM(size_bytes),0) AS t FROM cache_entries")
        return int(row["t"]) if row else 0

    def forget_cache(self, track_id: str) -> None:
        self.execute("DELETE FROM cache_entries WHERE track_id=?", (track_id,))

    # ------------------------------------------------------------- health
    def mark_provider(self, provider: str, healthy: bool, error: str | None = None) -> None:
        col = "last_success" if healthy else "last_failure"
        self.execute(
            f"INSERT INTO provider_health(provider, healthy, {col}, last_error) "
            f"VALUES(?,?,CURRENT_TIMESTAMP,?) "
            f"ON CONFLICT(provider) DO UPDATE SET healthy=excluded.healthy, "
            f"{col}=CURRENT_TIMESTAMP, last_error=excluded.last_error",
            (provider, 1 if healthy else 0, error),
        )

    def health_snapshot(self) -> dict[str, dict]:
        rows = self.fetchall("SELECT * FROM provider_health")
        return {
            r["provider"]: {
                "healthy": bool(r["healthy"]),
                "last_success": r["last_success"],
                "last_failure": r["last_failure"],
                "last_error": r["last_error"],
            }
            for r in rows
        }
