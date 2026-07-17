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
        has_video         INTEGER NOT NULL DEFAULT 0,  -- 1 if source is a video container (FFmpeg strips video)
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
    # ---- torrent client state --------------------------------------------
    # aria2 owns the actual download state. These tables are the durable
    # application-side index used by the dashboard and the torrent provider.
    """
    CREATE TABLE IF NOT EXISTS torrents (
        gid              TEXT PRIMARY KEY,
        name             TEXT NOT NULL DEFAULT '',
        info_hash        TEXT,
        source           TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT 'waiting',
        total_length     INTEGER NOT NULL DEFAULT 0,
        completed_length INTEGER NOT NULL DEFAULT 0,
        download_speed   INTEGER NOT NULL DEFAULT 0,
        upload_speed     INTEGER NOT NULL DEFAULT 0,
        error_code       TEXT,
        error_message    TEXT,
        added_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_torrents_status ON torrents(status)",
    """
    CREATE TABLE IF NOT EXISTS torrent_files (
        gid              TEXT NOT NULL,
        file_index       INTEGER NOT NULL,
        path             TEXT NOT NULL,
        length           INTEGER NOT NULL DEFAULT 0,
        completed_length INTEGER NOT NULL DEFAULT 0,
        selected         INTEGER NOT NULL DEFAULT 1,
        is_complete      INTEGER NOT NULL DEFAULT 0,
        playlist_enabled INTEGER NOT NULL DEFAULT 0,
        media_override   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(gid, file_index),
        FOREIGN KEY(gid) REFERENCES torrents(gid) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_torrent_files_playlist ON torrent_files(playlist_enabled)",
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
                self._apply_column_migrations(cur)
            finally:
                cur.close()

    def _apply_column_migrations(self, cur: sqlite3.Cursor) -> None:
        """Idempotent `ALTER TABLE ADD COLUMN` migrations.

        `CREATE TABLE IF NOT EXISTS` doesn't add new columns to an existing
        table — so anything added to the schema after the first release lands
        here. Each migration checks the column exists before ADDing.
        """
        cur.execute("PRAGMA table_info(tracks)")
        cols = {row[1] for row in cur.fetchall()}
        if "has_video" not in cols:
            cur.execute("ALTER TABLE tracks ADD COLUMN has_video INTEGER NOT NULL DEFAULT 0")

        cur.execute("PRAGMA table_info(torrent_files)")
        torrent_file_cols = {row[1] for row in cur.fetchall()}
        if "media_override" not in torrent_file_cols:
            cur.execute(
                "ALTER TABLE torrent_files ADD COLUMN media_override INTEGER NOT NULL DEFAULT 0"
            )

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

    def list_all(
        self, *, offset: int = 0, limit: int = 100, search: str | None = None
    ) -> list[sqlite3.Row]:
        """Return a non-wrapping track page with natural playlist positions.

        The ranking CTE runs before an optional title filter, so a searched
        result still reports its position in the full sequential playlist.
        Cache paths are joined in one read; listing does not touch LRU state.
        """
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 1000))
        sql = (
            "WITH ordered AS ("
            "SELECT tracks.*, ROW_NUMBER() OVER (ORDER BY sort_order, track_id) - 1 "
            "AS playlist_position FROM tracks) "
            "SELECT ordered.*, cache_entries.file_path AS cache_file_path "
            "FROM ordered LEFT JOIN cache_entries ON cache_entries.track_id = ordered.track_id "
        )
        params: tuple[object, ...]
        if search:
            sql += "WHERE ordered.title LIKE ? "
            params = (f"%{search}%", limit, offset)
        else:
            params = (limit, offset)
        sql += "ORDER BY ordered.playlist_position LIMIT ? OFFSET ?"
        return self.fetchall(sql, params)

    def count_tracks(self, *, search: str | None = None) -> int:
        """Return the number of tracks matching an optional title filter."""
        if search:
            row = self.fetchone(
                "SELECT COUNT(*) AS n FROM tracks WHERE title LIKE ?", (f"%{search}%",)
            )
        else:
            row = self.fetchone("SELECT COUNT(*) AS n FROM tracks")
        return int(row["n"]) if row else 0

    def position_of(self, track_id: str) -> int | None:
        """Return a track's zero-based position in natural playlist order."""
        row = self.fetchone("SELECT sort_order FROM tracks WHERE track_id=?", (track_id,))
        if row is None:
            return None
        count = self.fetchone(
            "SELECT COUNT(*) AS n FROM tracks "
            "WHERE sort_order < ? OR (sort_order = ? AND track_id < ?)",
            (row["sort_order"], row["sort_order"], track_id),
        )
        return int(count["n"]) if count else 0

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
                        "provider, source_ref, sort_order, has_video) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            t["track_id"],
                            t["title"],
                            int(t.get("duration_seconds") or 0),
                            int(t.get("size_bytes") or 0),
                            t["provider"],
                            t["source_ref"],
                            float(t["sort_order"]),
                            1 if t.get("has_video") else 0,
                        ),
                    )
                    added += 1
                else:
                    cur.execute(
                        "UPDATE tracks SET title=?, duration_seconds=?, size_bytes=?, "
                        "sort_order=?, has_video=? WHERE provider=? AND source_ref=?",
                        (
                            t["title"],
                            int(t.get("duration_seconds") or 0),
                            int(t.get("size_bytes") or 0),
                            float(t["sort_order"]),
                            1 if t.get("has_video") else 0,
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

    # ------------------------------------------------------------- torrents
    def upsert_torrent(self, torrent: dict) -> None:
        """Persist the latest status reported by the torrent client."""
        self.execute(
            """
            INSERT INTO torrents(
                gid, name, info_hash, source, status, total_length,
                completed_length, download_speed, upload_speed, error_code,
                error_message
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(gid) DO UPDATE SET
                name=CASE
                    WHEN excluded.name != '' THEN excluded.name
                    WHEN substr(torrents.name, 1, 10) = '[METADATA]' THEN ''
                    ELSE torrents.name
                END,
                info_hash=COALESCE(excluded.info_hash, torrents.info_hash),
                status=excluded.status,
                total_length=excluded.total_length,
                completed_length=excluded.completed_length,
                download_speed=excluded.download_speed,
                upload_speed=excluded.upload_speed,
                error_code=excluded.error_code,
                error_message=excluded.error_message,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                torrent["gid"],
                torrent.get("name") or "",
                torrent.get("info_hash"),
                torrent.get("source") or "",
                torrent.get("status") or "waiting",
                int(torrent.get("total_length") or 0),
                int(torrent.get("completed_length") or 0),
                int(torrent.get("download_speed") or 0),
                int(torrent.get("upload_speed") or 0),
                str(torrent.get("error_code")) if torrent.get("error_code") else None,
                torrent.get("error_message"),
            ),
        )

    def list_torrents(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM torrents ORDER BY added_at DESC, gid")

    def torrent(self, gid: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM torrents WHERE gid=?", (gid,))

    def upsert_torrent_file(self, gid: str, file_info: dict) -> None:
        self.execute(
            """
            INSERT INTO torrent_files(
                gid, file_index, path, length, completed_length, selected, is_complete
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(gid, file_index) DO UPDATE SET
                path=excluded.path,
                length=excluded.length,
                completed_length=excluded.completed_length,
                selected=excluded.selected,
                is_complete=excluded.is_complete
            """,
            (
                gid,
                int(file_info["file_index"]),
                str(file_info.get("path") or ""),
                int(file_info.get("length") or 0),
                int(file_info.get("completed_length") or 0),
                1 if file_info.get("selected", True) else 0,
                1 if file_info.get("is_complete") else 0,
            ),
        )

    def torrent_files(self, gid: str) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM torrent_files WHERE gid=? ORDER BY file_index", (gid,)
        )

    def torrent_file(self, gid: str, file_index: int) -> sqlite3.Row | None:
        return self.fetchone(
            "SELECT * FROM torrent_files WHERE gid=? AND file_index=?", (gid, int(file_index))
        )

    def set_torrent_file_enabled(
        self, gid: str, file_index: int, enabled: bool, *, media_override: bool = False
    ) -> None:
        cur = self.execute(
            "UPDATE torrent_files SET playlist_enabled=?, media_override=? "
            "WHERE gid=? AND file_index=?",
            (1 if enabled else 0, 1 if enabled and media_override else 0, gid, int(file_index)),
        )
        if cur.rowcount == 0:
            raise KeyError(f"unknown torrent file {gid}/{file_index}")

    def selected_torrent_files(self) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT torrent_files.*, torrents.name AS torrent_name, torrents.status AS torrent_status
            FROM torrent_files
            JOIN torrents ON torrents.gid = torrent_files.gid
            WHERE torrent_files.playlist_enabled=1
            ORDER BY torrents.added_at, torrent_files.gid, torrent_files.file_index
            """
        )

    def remove_torrent(self, gid: str) -> list[tuple[str, str]]:
        """Remove a torrent and its playlist/cache rows.

        Returns ``(track_id, cache_path)`` pairs so the service can unlink the
        cached bytes after the SQL transaction has completed.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                SELECT tracks.track_id, cache_entries.file_path
                FROM tracks
                LEFT JOIN cache_entries ON cache_entries.track_id=tracks.track_id
                WHERE tracks.provider='torrent' AND tracks.source_ref LIKE ?
                """,
                (f"{gid}:%",),
            )
            removed = [(r[0], r[1]) for r in cur.fetchall()]
            cur.execute(
                "DELETE FROM cache_entries WHERE track_id IN "
                "(SELECT track_id FROM tracks WHERE provider='torrent' AND source_ref LIKE ?)",
                (f"{gid}:%",),
            )
            cur.execute(
                "DELETE FROM tracks WHERE provider='torrent' AND source_ref LIKE ?", (f"{gid}:%",)
            )
            cur.execute("DELETE FROM torrent_files WHERE gid=?", (gid,))
            cur.execute("DELETE FROM torrents WHERE gid=?", (gid,))
        return removed

    def remove_provider_tracks_not_in(
        self, provider: str, source_refs: set[str]
    ) -> list[tuple[str, str]]:
        """Remove stale rows after a successful authoritative provider scan."""
        with self.transaction() as cur:
            if source_refs:
                placeholders = ",".join("?" for _ in source_refs)
                args: tuple[object, ...] = (provider, *sorted(source_refs))
                where = f"provider=? AND source_ref NOT IN ({placeholders})"
            else:
                args = (provider,)
                where = "provider=?"
            cur.execute(
                f"SELECT tracks.track_id, cache_entries.file_path FROM tracks "
                f"LEFT JOIN cache_entries ON cache_entries.track_id=tracks.track_id WHERE {where}",
                args,
            )
            removed = [(r[0], r[1]) for r in cur.fetchall()]
            cur.execute(
                f"DELETE FROM cache_entries WHERE track_id IN "
                f"(SELECT track_id FROM tracks WHERE {where})",
                args,
            )
            cur.execute(f"DELETE FROM tracks WHERE {where}", args)
        return removed

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
