"""End-to-end: dashboard enqueues a control command, bot's scheduler drains it.

Uses a fake player that records skip/pause/resume calls. Verifies the shared
SQLite queue is the correct integration point between the two services.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.scheduler import Scheduler
from bot.tracker import SessionTracker
from dashboard import commands as cmd_queue
from dashboard.auth import SessionSigner
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from db.database import Database


@pytest.fixture
def shared_db(tmp_path: Path) -> Database:
    """One DB, opened twice — like bot + dashboard sharing a mount."""
    db = Database(tmp_path / "shared.db")
    yield db
    db.close()


@pytest.fixture
def dashboard_client(shared_db: Database):
    cfg = DashboardConfig(
        port=8000,
        secret_key="k" * 32,
        database_path=":memory:",
        file_provider_base_url="http://p",
        discord_client_id="cid",
        discord_client_secret="sec",
        discord_redirect_uri="http://cb",
        admin_user_ids=frozenset({"111"}),
    )
    # Reuse the exact same DB the bot will read from.
    app = create_app(cfg, db=shared_db, http_client=None)
    client = TestClient(app, follow_redirects=False)
    signer = SessionSigner(cfg.secret_key)
    admin_token = signer.encode({"user_id": "111", "username": "Admin", "csrf": "tok"})
    client.cookies.set("tvbot_session", admin_token)
    return client


class RecordingPlayer:
    """Fake player that just records what the scheduler asks it to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def skip(self) -> None:
        self.calls.append("skip")

    async def pause(self) -> None:
        self.calls.append("pause")

    async def resume(self) -> None:
        self.calls.append("resume")


async def test_dashboard_skip_drains_to_player(
    shared_db: Database, dashboard_client: TestClient
) -> None:
    """Full round-trip: POST /controls → enqueued → scheduler.drain_commands → player.skip."""
    player = RecordingPlayer()

    async def handler(command: str, _payload) -> str:
        if command == "skip":
            await player.skip()
            return "ok:skipped"
        if command == "pause":
            await player.pause()
            return "ok:paused"
        if command == "resume":
            await player.resume()
            return "ok:resumed"
        return f"error: unknown {command}"

    scheduler = Scheduler(
        db=shared_db,
        tracker=SessionTracker(shared_db),
        command_handler=handler,
    )

    # Post from the dashboard side.
    r = dashboard_client.post("/controls", data={"action": "skip", "csrf": "tok"})
    assert r.status_code == 303

    # Queue should have one pending command.
    pending = cmd_queue.pending(shared_db)
    assert len(pending) == 1
    assert pending[0].command == "skip"

    # Bot side: drain.
    n = await scheduler.drain_commands()
    assert n == 1

    # Player got the call.
    assert player.calls == ["skip"]

    # Queue is now empty.
    assert cmd_queue.pending(shared_db) == []

    # And the row is marked with the result.
    recent = cmd_queue.recent(shared_db)
    assert recent[0].result == "ok:skipped"
    assert recent[0].executed_at is not None


async def test_multiple_commands_processed_in_order(
    shared_db: Database, dashboard_client: TestClient
) -> None:
    player = RecordingPlayer()

    async def handler(cmd: str, _p) -> str:
        await getattr(player, cmd)()
        return "ok"

    scheduler = Scheduler(
        db=shared_db,
        tracker=SessionTracker(shared_db),
        command_handler=handler,
    )

    for action in ("pause", "resume", "skip"):
        dashboard_client.post("/controls", data={"action": action, "csrf": "tok"})

    await scheduler.drain_commands()
    assert player.calls == ["pause", "resume", "skip"]


async def test_unknown_action_never_reaches_bot(
    shared_db: Database, dashboard_client: TestClient
) -> None:
    r = dashboard_client.post("/controls", data={"action": "drop_database", "csrf": "tok"})
    assert r.status_code == 400  # rejected at dashboard boundary
    assert cmd_queue.pending(shared_db) == []
