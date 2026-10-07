"""Race tests for the storm fix: the bot must not fight discord.py.

The 2026-10-07 storm came from two reconnect paths tearing down each other's
handshakes. These tests drive ``ensure_station_voice_connected`` through the
exact shape of that race:

* ``station.voice_client`` is None (our handler cleared it) while
  ``guild.voice_client`` is reconnecting — the bot must adopt, not disconnect.
* Two concurrent callers must produce exactly one ``channel.connect``.
"""

from __future__ import annotations

import asyncio

from bot.main import ensure_station_voice_connected
from bot.voice_reconnect import GuildReconnectGuard


class FakeRunner:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class FakeConnection:
    def __init__(self, done: bool) -> None:
        self.runner = FakeRunner(done)


class FakeVoiceClient:
    def __init__(self, *, connected: bool, reconnecting: bool = False) -> None:
        self._connected = connected
        self._connection = FakeConnection(done=not reconnecting)
        self.disconnect_calls: list[dict] = []
        self.connected_since = None

    def is_connected(self) -> bool:
        return self._connected

    async def disconnect(self, **kwargs):  # type: ignore[no-untyped-def]
        self.disconnect_calls.append(kwargs)
        self._connected = False


class FakeChannel:
    def __init__(self, client: FakeVoiceClient) -> None:
        self._client = client
        self.connect_calls = 0

    async def connect(self, **kwargs):  # type: ignore[no-untyped-def]
        self.connect_calls += 1
        self._client._connected = True
        self._client.last_connect_kwargs = kwargs
        return self._client


class FakeGuild:
    def __init__(self, channel: FakeChannel, voice_client) -> None:  # type: ignore[no-untyped-def]
        self._channel = channel
        self.voice_client = voice_client

    def get_channel(self, channel_id: int):  # type: ignore[no-untyped-def]
        return self._channel


class FakeClient:
    def __init__(self, guild: FakeGuild) -> None:
        self._guild = guild

    def get_guild(self, guild_id: int):  # type: ignore[no-untyped-def]
        return self._guild


def make_station(guild_id: str, voice_client, player_client=None):  # type: ignore[no-untyped-def]
    player = type("P", (), {"voice_client": player_client})()
    return type(
        "S",
        (),
        {
            "voice_client": voice_client,
            "guild_id": guild_id,
            "voice_channel_id": 10,
            "voice_channel": None,
            "player": player,
        },
    )()


class TestStormRace:
    async def test_adopts_discord_reconnecting_client_without_disconnecting(self) -> None:
        """station.voice_client is None, guild.voice_client is mid-reconnect."""
        reconnecting = FakeVoiceClient(connected=False, reconnecting=True)
        channel = FakeChannel(FakeVoiceClient(connected=False))
        guild = FakeGuild(channel, reconnecting)
        client = FakeClient(guild)
        station = make_station("1", None)

        async def fake_sleep(_seconds: float) -> None:
            reconnecting._connected = True  # discord.py finishes during grace

        ok = await ensure_station_voice_connected(
            client,
            station,
            guard=GuildReconnectGuard(),
            sleep=fake_sleep,
        )

        assert ok is True
        assert station.voice_client is reconnecting
        assert station.player.voice_client is reconnecting
        assert reconnecting.disconnect_calls == []
        assert channel.connect_calls == 0, "must not start a competing handshake"

    async def test_replaces_stale_client_when_discord_gave_up(self) -> None:
        stale = FakeVoiceClient(connected=False, reconnecting=False)
        fresh = FakeVoiceClient(connected=False)
        channel = FakeChannel(fresh)
        guild = FakeGuild(channel, stale)
        client = FakeClient(guild)
        station = make_station("1", stale)

        ok = await ensure_station_voice_connected(client, station, guard=GuildReconnectGuard())

        assert ok is True
        assert channel.connect_calls == 1
        assert len(stale.disconnect_calls) == 1
        assert stale.disconnect_calls[0].get("force") is True
        assert stale.disconnect_calls[0].get("wait") is True
        assert station.voice_client is fresh

    async def test_caller_that_sees_none_while_reconnecting_does_not_connect(self) -> None:
        """The original bug: caller saw voice_client None and force-reconnected."""
        reconnecting = FakeVoiceClient(connected=False, reconnecting=True)
        channel = FakeChannel(FakeVoiceClient(connected=False))
        guild = FakeGuild(channel, reconnecting)
        client = FakeClient(guild)
        station = make_station("1", None)

        async def fake_sleep(_seconds: float) -> None:
            reconnecting._connected = True

        ok = await ensure_station_voice_connected(
            client, station, guard=GuildReconnectGuard(), sleep=fake_sleep
        )

        assert ok is True
        assert channel.connect_calls == 0
        assert reconnecting.disconnect_calls == []

    async def test_concurrent_callers_connect_once(self) -> None:
        fresh = FakeVoiceClient(connected=False)
        channel = FakeChannel(fresh)
        guild = FakeGuild(channel, None)
        client = FakeClient(guild)
        station = make_station("1", None)
        guard = GuildReconnectGuard()

        started = asyncio.Event()

        async def slow_connect(**kwargs):  # type: ignore[no-untyped-def]
            started.set()
            await asyncio.sleep(0.05)
            channel.connect_calls += 1
            fresh._connected = True
            return fresh

        channel.connect = slow_connect  # type: ignore[method-assign]

        first = asyncio.create_task(
            ensure_station_voice_connected(client, station, guard=guard)
        )
        await started.wait()
        second = asyncio.create_task(
            ensure_station_voice_connected(client, station, guard=guard)
        )
        results = await asyncio.gather(first, second)

        assert results == [True, True]
        assert channel.connect_calls == 1
