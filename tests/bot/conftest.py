"""Shared fixtures for bot tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.state import BotState
from db.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "bot.db")
    yield d
    d.close()


@pytest.fixture
def state(db: Database) -> BotState:
    return BotState(db)
