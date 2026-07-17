"""Shared SQLite persistence layer for bot + dashboard.

The database file is a single SQLite file (see `DATABASE_PATH`), opened in
WAL mode so the bot process (writer) and dashboard process (reader) can share
it safely from separate containers.
"""

from db.database import Database, connect, get_default_path

__all__ = ["Database", "connect", "get_default_path"]
