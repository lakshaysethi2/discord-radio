"""Shared fixtures for db tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from db.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.db")
    yield d
    d.close()
