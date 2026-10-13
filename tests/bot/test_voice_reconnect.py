"""Tests for the per-guild voice reconnect guard (2026-10-07 storm fix).

The storm was our own code fighting discord.py: the watchdog and the
voice_state_update handler both called ``ensure_station_voice_connected``,
which force-disconnected and reconnected. Each path killed the other's
connection every ~30s. These tests pin the fix:

* concurrent reconnects for one guild run exactly once,
* a discord.py reconnect in progress is adopted, not torn down,
* a stale client (discord.py gave up) is still replaced.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.voice_reconnect import (
    GuildReconnectGuard,
    is_discord_reconnecting,
    is_voice_connected,
    wait_for_discord_reconnect,
)


class FakeRunner:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class FakeConnection:
    def __init__(self, runner: FakeRunner | None = None) -> None:
        self._runner = runner


class FakeVoiceClient:
    def __init__(self, connected: bool = False, reconnecting: bool = False) -> None:
        self._connected = connected
        self.connection = FakeConnection(FakeRunner(done=not reconnecting))

    def is_connected(self) -> bool:
        return self._connected


def attach_connection(vc: FakeVoiceClient) -> FakeVoiceClient:
    """Expose the fake connection as the private attr discord.py uses."""
    vc._connection = vc.connection  # type: ignore[attr-defined]
    return vc


class TestHelpers:
    def test_is_voice_connected_none(self) -> None:
        assert is_voice_connected(None) is False

    def test_is_voice_connected_true(self) -> None:
        assert is_voice_connected(FakeVoiceClient(connected=True)) is True

    def test_is_discord_reconnecting_true(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=False, reconnecting=True))
        assert is_discord_reconnecting(vc) is True

    def test_is_discord_reconnecting_false_when_runner_done(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=False, reconnecting=False))
        assert is_discord_reconnecting(vc) is False

    async def test_is_discord_reconnecting_uses_real_discord_attr(self, monkeypatch) -> None:
        """Exercise actual VoiceConnectionState fields, without Discord I/O."""
        from types import SimpleNamespace

        import discord
        from discord.voice_state import SocketReader

        # Construction normally starts a socket-reader thread; no sockets are
        # needed to check the library's task ownership contract.
        monkeypatch.setattr(SocketReader, "start", lambda self: None)
        async with discord.Client(intents=discord.Intents.none()) as client:
            vc = discord.VoiceClient(client, SimpleNamespace())
            assert vc._connection._runner is None
            assert vc._connection._connector is None
            assert not is_discord_reconnecting(vc)
            task = asyncio.create_task(asyncio.sleep(0))
            vc._connection._runner = task
            assert is_discord_reconnecting(vc)
            await task
            assert not is_discord_reconnecting(vc)

    def test_is_discord_reconnecting_none_client(self) -> None:
        assert is_discord_reconnecting(None) is False


class TestGuildReconnectGuard:
    async def test_concurrent_calls_run_factory_once(self) -> None:
        guard = GuildReconnectGuard()
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory() -> bool:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return True

        first = asyncio.create_task(guard.run("g1", factory))
        await started.wait()
        second = asyncio.create_task(guard.run("g1", factory))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)

        assert results == [True, True]
        assert calls == 1

    async def test_sequential_calls_run_factory_each_time(self) -> None:
        guard = GuildReconnectGuard()
        calls = 0

        async def factory() -> bool:
            nonlocal calls
            calls += 1
            return True

        assert await guard.run("g1", factory) is True
        assert await guard.run("g1", factory) is True
        assert calls == 2

    async def test_different_guilds_run_independently(self) -> None:
        guard = GuildReconnectGuard()
        seen: list[str] = []

        def make(name: str):
            async def factory() -> bool:
                seen.append(name)
                return True

            return factory

        await asyncio.gather(guard.run("a", make("a")), guard.run("b", make("b")))
        assert sorted(seen) == ["a", "b"]

    async def test_inflight_tracks_running_task(self) -> None:
        guard = GuildReconnectGuard()
        release = asyncio.Event()

        async def factory() -> bool:
            await release.wait()
            return True

        task = asyncio.create_task(guard.run("g1", factory))
        await asyncio.sleep(0)
        assert guard.inflight("g1") is True
        release.set()
        await task
        await asyncio.sleep(0)
        assert guard.inflight("g1") is False

    async def test_factory_exception_surfaces_and_clears(self) -> None:
        guard = GuildReconnectGuard()

        async def boom() -> bool:
            raise RuntimeError("connect failed")

        with pytest.raises(RuntimeError):
            await guard.run("g1", boom)
        await asyncio.sleep(0)
        assert guard.inflight("g1") is False


class TestWaitForDiscordReconnect:
    async def test_adopts_already_connected_client(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=True, reconnecting=True))
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        ok = await wait_for_discord_reconnect(lambda: vc, sleep=fake_sleep)
        assert ok is True
        assert slept == []

    async def test_waits_while_discord_reconnecting_then_adopts(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=False, reconnecting=True))

        async def fake_sleep(seconds: float) -> None:
            vc._connected = True  # discord.py finishes during the grace period

        ok = await wait_for_discord_reconnect(
            lambda: vc, grace_seconds=8.0, sleep=fake_sleep
        )
        assert ok is True

    async def test_gives_up_when_discord_not_reconnecting(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=False, reconnecting=False))

        async def fake_sleep(seconds: float) -> None:
            raise AssertionError("should not sleep when discord.py is not reconnecting")

        ok = await wait_for_discord_reconnect(lambda: vc, sleep=fake_sleep)
        assert ok is False

    async def test_gives_up_after_grace_when_run_never_finishes(self) -> None:
        vc = attach_connection(FakeVoiceClient(connected=False, reconnecting=True))
        calls = 0

        async def fake_sleep(seconds: float) -> None:
            nonlocal calls
            calls += 1

        ok = await wait_for_discord_reconnect(
            lambda: vc, grace_seconds=1.0, sleep=fake_sleep
        )
        assert ok is False
        assert calls >= 1


async def test_cancelling_a_waiter_does_not_abort_the_shared_reconnect() -> None:
    guard = GuildReconnectGuard()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> bool:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return True

    first = asyncio.create_task(guard.run("g", factory))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert guard.inflight("g")
    second = asyncio.create_task(guard.run("g", factory))
    await asyncio.sleep(0)
    release.set()
    assert await second
    assert calls == 1


async def test_connector_gets_grace_before_websocket_runner_exists() -> None:
    from types import SimpleNamespace

    vc = attach_connection(FakeVoiceClient())
    vc._connection = SimpleNamespace(_runner=None, _connector=FakeRunner(done=False))
    assert is_discord_reconnecting(vc)

    async def finish(_seconds: float) -> None:
        vc._connected = True

    assert await wait_for_discord_reconnect(lambda: vc, sleep=finish)


async def test_teardown_joins_old_tasks_and_uses_real_public_api() -> None:
    from types import SimpleNamespace
    from unittest.mock import create_autospec

    import discord

    from bot.voice_reconnect import disconnect_stale_voice_client

    events = []

    async def old_task(name: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            # Cleanup is asynchronous, just like the real connector.
            await asyncio.sleep(0)
            events.append(name)

    runner = asyncio.create_task(old_task("runner"))
    connector = asyncio.create_task(old_task("connector"))
    await asyncio.sleep(0)
    vc = create_autospec(discord.VoiceClient, instance=True)
    vc._connection = SimpleNamespace(_runner=runner, _connector=connector)

    async def disconnected(*, force: bool = False) -> None:
        assert force
        assert runner.done() and connector.done()
        events.append("disconnect")

    vc.disconnect.side_effect = disconnected
    await disconnect_stale_voice_client(vc)
    vc.disconnect.assert_awaited_once_with(force=True)
    assert set(events[:2]) == {"runner", "connector"}
    assert events[-1] == "disconnect"
