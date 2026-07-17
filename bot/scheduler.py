"""Background tasks: hourly checkpoints + monthly reset (§6.3, §9).

Uses `asyncio.create_task`. Nothing here depends on discord.py — the scheduler
just needs a DB and a `SessionTracker`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from bot.milestones import MilestoneAnnouncer, MilestoneChecker
from bot.tracker import SessionTracker, month_key_for
from dashboard import commands as cmd_queue
from db.database import Database
from db.models import BotStateKey

CommandHandler = Callable[[str, dict | None], Awaitable[str]]

log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def monthly_reset_due(db: Database, now: datetime | None = None) -> bool:
    """True if the current month is later than the one we last snapshot-reset."""
    now = now or _now_utc()
    last = db.get_state(BotStateKey.LAST_MONTHLY_RESET) or ""
    return month_key_for(now) != last


def run_monthly_reset(
    db: Database,
    now: datetime | None = None,
) -> dict:
    """Snapshot last month's monthly totals then zero them out.

    Called by the scheduler once per hour; internally checks `monthly_reset_due`.
    Returns a summary dict (also useful in tests).
    """
    now = now or _now_utc()
    if not monthly_reset_due(db, now):
        return {"ran": False, "reason": "already_done"}

    # We snapshot whatever month_key each row currently has (NOT the "current"
    # month) — that way, if the bot was offline for a whole month, we still
    # archive that older month before overwriting it.
    prev_rows = db.fetchall(
        "SELECT user_id, username, month_key, total_seconds_monthly "
        "FROM user_totals WHERE total_seconds_monthly > 0 AND month_key IS NOT NULL"
    )
    # Rank per month_key.
    per_month: dict[str, list] = {}
    for r in prev_rows:
        per_month.setdefault(r["month_key"], []).append(r)

    inserted = 0
    with db.transaction() as cur:
        for mkey, rows in per_month.items():
            rows.sort(key=lambda r: int(r["total_seconds_monthly"] or 0), reverse=True)
            for rank, r in enumerate(rows, start=1):
                cur.execute(
                    "INSERT INTO monthly_snapshots(user_id, username, month_key, "
                    "total_seconds, rank) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(user_id, month_key) DO UPDATE SET "
                    "username=excluded.username, "
                    "total_seconds=excluded.total_seconds, rank=excluded.rank",
                    (r["user_id"], r["username"], mkey, int(r["total_seconds_monthly"] or 0), rank),
                )
                inserted += 1
        cur.execute(
            "UPDATE user_totals SET total_seconds_monthly = 0, month_key = ?",
            (month_key_for(now),),
        )
        cur.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (BotStateKey.LAST_MONTHLY_RESET, month_key_for(now)),
        )
    return {
        "ran": True,
        "month_key": month_key_for(now),
        "snapshotted_rows": inserted,
        "months_snapshotted": list(per_month.keys()),
    }


class Scheduler:
    """Owns the recurring background tasks."""

    def __init__(
        self,
        *,
        db: Database,
        tracker: SessionTracker,
        milestones: MilestoneAnnouncer | None = None,
        checkpoint_interval_seconds: int = 3600,
        monthly_check_interval_seconds: int = 3600,
        command_poll_interval_seconds: int = 2,
        command_handler: CommandHandler | None = None,
    ) -> None:
        self.db = db
        self.tracker = tracker
        self.milestones = milestones
        # For pure-logic paths (tests, dashboard). Announcer wraps this.
        self._checker = MilestoneChecker(db) if milestones is None else milestones.checker
        self.checkpoint_interval_seconds = checkpoint_interval_seconds
        self.monthly_check_interval_seconds = monthly_check_interval_seconds
        self.command_poll_interval_seconds = command_poll_interval_seconds
        self.command_handler = command_handler
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if self._tasks:
            return
        # get_running_loop requires an active loop, which is guaranteed since
        # scheduler.start() is only ever called from inside on_ready (async).
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._checkpoint_loop(), name="checkpoint-loop"))
        self._tasks.append(loop.create_task(self._monthly_loop(), name="monthly-loop"))
        if self.command_handler is not None:
            self._tasks.append(loop.create_task(self._command_loop(), name="command-loop"))

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def _checkpoint_loop(self) -> None:  # pragma: no cover — timing-heavy
        try:
            while True:
                await asyncio.sleep(self.checkpoint_interval_seconds)
                try:
                    n = self.tracker.checkpoint_open_sessions()
                    if n:
                        log.info("checkpointed %d open sessions", n)
                    # After checkpoints, check milestones for everyone with an open session.
                    for row in self.tracker.open_sessions():
                        await self._maybe_announce(row["user_id"])
                except Exception:
                    log.exception("checkpoint loop iteration failed")
        except asyncio.CancelledError:
            pass

    async def _monthly_loop(self) -> None:  # pragma: no cover — timing-heavy
        try:
            while True:
                try:
                    summary = run_monthly_reset(self.db)
                    if summary.get("ran"):
                        log.info("monthly reset: %s", summary)
                        # No milestone check needed — resets only zero monthly counter.
                except Exception:
                    log.exception("monthly loop iteration failed")
                await asyncio.sleep(self.monthly_check_interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _maybe_announce(self, user_id: str) -> None:
        if self.milestones is not None:
            await self.milestones.check_and_announce(user_id)
        else:
            # No announcer wired up (tests) — still flip the flags so the DB
            # stays consistent.
            self._checker.check_user(user_id)

    async def _command_loop(self) -> None:  # pragma: no cover — timing-heavy
        try:
            while True:
                await asyncio.sleep(self.command_poll_interval_seconds)
                try:
                    await self.drain_commands()
                except Exception:
                    log.exception("command loop iteration failed")
        except asyncio.CancelledError:
            pass

    async def drain_commands(self) -> int:
        """Execute any pending dashboard_commands rows. Returns count run."""
        if self.command_handler is None:
            return 0
        pending = cmd_queue.pending(self.db)
        for row in pending:
            try:
                result = await self.command_handler(row.command, row.payload)
            except Exception as exc:
                result = f"error: {exc}"
                log.exception("command %s (%s) failed", row.command_id, row.command)
            cmd_queue.mark_done(self.db, row.command_id, result=result or "ok")
        return len(pending)
