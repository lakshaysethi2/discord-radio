"""SQLite connection factory + migration runner.

Design decisions:

* One shared file across bot + dashboard (per §14). WAL mode + a modest busy
  timeout is enough for the tiny write volumes we expect (a session close every
  few minutes, dashboard reads on demand).
* No ORM. Plain `sqlite3` with `Row` factory. Every query is co-located with
  the module that owns that table's semantics (tracker.py, state.py, etc.),
  which keeps behavior easy to trace.
* Migrations are just idempotent `CREATE TABLE IF NOT EXISTS` executed on
  open. If we ever need real migrations we can bolt on a `schema_version`
  table without breaking anything.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from db.models import SCHEMA

_DEFAULT_ENV_KEY = "DATABASE_PATH"
_DEFAULT_PATH = "./data/tv.db"


def get_default_path() -> str:
    """Read `DATABASE_PATH` from env or fall back to `./data/tv.db`."""
    return os.environ.get(_DEFAULT_ENV_KEY, _DEFAULT_PATH)


class Database:
    """Thin wrapper around a `sqlite3.Connection` with WAL + migrations.

    The wrapper is intentionally minimal — most callers just want
    `.execute(...)`, `.executemany(...)`, or the `.transaction()`
    context manager. All writes go through a single connection, guarded
    by a lock, which is fine for our write volume.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = str(path) if path is not None else get_default_path()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` because our dashboard is async and
        # sometimes touches the DB from a worker thread. The lock below
        # serializes writes; SQLite handles reads concurrently under WAL.
        self._conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage txns explicitly
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._configure_pragmas()
            self.migrate()
        except Exception:
            # Don't leak the connection if migration blows up on open.
            self._conn.close()
            raise

    # -------------------------------------------------------- context manager
    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ setup
    def _configure_pragmas(self) -> None:
        cur = self._conn.cursor()
        # WAL survives crashes better and allows readers to not block writers.
        # :memory: doesn't support WAL — skip gracefully.
        if self.path != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def migrate(self) -> None:
        """Apply the schema DDLs. Safe to call multiple times."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                for stmt in SCHEMA:
                    cur.execute(stmt)
                # Backfill any columns added after a DB was first created.
                # watch_sessions gained `guild_id` for multi-server tracking —
                # existing rows default to '' (the "unspecified" guild).
                self._ensure_column(cur, "watch_sessions", "guild_id", "TEXT NOT NULL DEFAULT ''")
                # guild_channels gained `parent_id` so we can default a server's
                # *Now Playing* posts to the voice channel's own text chat.
                self._ensure_column(cur, "guild_channels", "parent_id", "TEXT")
            finally:
                cur.close()

    @staticmethod
    def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
        """Add `column` to `table` if it isn't there yet (idempotent)."""
        existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # -------------------------------------------------------------- primitives
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """Execute a single statement under the write lock."""
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Iterable[tuple | dict]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, seq_of_params)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Wrap a block of writes in an explicit transaction.

        Because we opened the connection in autocommit (`isolation_level=None`)
        we begin the transaction manually. Rollback on exception, commit on
        clean exit.
        """
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
            self._conn.close()

    # ------------------------------------------------------------ bot_state I/O
    # Small key/value helpers. `bot_state` is a single-row-per-key table used
    # by both bot and dashboard, so it belongs on the base connection.

    def set_state(self, key: str, value: str | int | float | bool | None) -> None:
        v = "" if value is None else str(value)
        self.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, v),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.fetchone("SELECT value FROM bot_state WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    def get_state_int(self, key: str, default: int = 0) -> int:
        v = self.get_state(key)
        try:
            return int(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def get_state_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_state(key)
        if v is None or v == "":
            return default
        return v.lower() in ("1", "true", "yes", "on")


def connect(path: str | os.PathLike[str] | None = None) -> Database:
    """Convenience shim so callers can write `db.connect()`."""
    return Database(path)
