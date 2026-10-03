"""Tests for the pure-ish helpers in bot.main that don't need discord.py."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bot.main import (
    RadioClock,
    _resume_or_start,
    ensure_station_voice_connected,
    recover_silent_playback,
)
from bot.state import BotState
from provider.client import ProviderError, TrackResponse


def make_track(**kw) -> TrackResponse:
    base = {
        "track_id": "t1",
        "title": "T1",
        "duration_seconds": 300,
        "local_path": "/cache/t1.mp3",
        "provider_used": "local",
        "playlist_position": 0,
        "ready": True,
    }
    base.update(kw)
    return TrackResponse(**base)


class FakePlayer:
    def __init__(self, playing: bool = False) -> None:
        self.started: list[tuple[TrackResponse, float]] = []
        self._playing = playing

    def is_playing(self) -> bool:
        return self._playing

    async def start(self, track: TrackResponse, *, seek_seconds: float = 0.0) -> None:
        self.started.append((track, seek_seconds))
        self._playing = True


class NeverPlayingPlayer(FakePlayer):
    """FakePlayer whose start() never actually produces audio."""

    async def start(self, track: TrackResponse, *, seek_seconds: float = 0.0) -> None:
        self.started.append((track, seek_seconds))


class ScriptedProvider:
    """Provider that returns a series of responses (raises included) in order."""

    def __init__(
        self, current_seq: list, by_id_seq: list | None = None, next_seq: list | None = None
    ) -> None:
        self._current = list(current_seq)
        self._by_id = list(by_id_seq or [])
        self._next = list(next_seq or [])

    async def current(self) -> TrackResponse:
        item = self._current.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def next(self) -> TrackResponse:
        item = self._next.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get_by_id(self, tid: str) -> TrackResponse:
        item = self._by_id.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestResumeOrStart:
    async def test_starts_when_provider_ready(self, state: BotState) -> None:
        player = FakePlayer()
        prov = ScriptedProvider([make_track(title="Fresh")])
        await _resume_or_start(player, prov, state, initial_backoff=0.001, max_backoff=0.001)  # type: ignore[arg-type]
        assert len(player.started) == 1
        assert player.started[0][0].title == "Fresh"
        assert player.started[0][1] == 0.0

    async def test_resumes_saved_track(self, state: BotState) -> None:
        state.current_track_id = "t1"
        state.playback_position_seconds = 42
        player = FakePlayer()
        prov = ScriptedProvider(current_seq=[], by_id_seq=[make_track()])
        await _resume_or_start(player, prov, state, initial_backoff=0.001)  # type: ignore[arg-type]
        assert player.started[0][1] == 42.0

    async def test_falls_back_to_current_when_saved_not_ready(self, state: BotState) -> None:
        state.current_track_id = "t1"
        state.playback_position_seconds = 42
        player = FakePlayer()
        prov = ScriptedProvider(
            current_seq=[make_track(title="FromCurrent")],
            by_id_seq=[make_track(ready=False, local_path="")],
        )
        await _resume_or_start(player, prov, state, initial_backoff=0.001)  # type: ignore[arg-type]
        assert player.started[0][0].title == "FromCurrent"
        assert player.started[0][1] == 0

    async def test_retries_then_succeeds(self, state: BotState) -> None:
        player = FakePlayer()
        prov = ScriptedProvider(
            [
                ProviderError("not ready 1"),
                ProviderError("not ready 2"),
                make_track(),
            ]
        )
        await _resume_or_start(
            player,
            prov,
            state,  # type: ignore[arg-type]
            initial_backoff=0.001,
            max_backoff=0.001,
        )
        assert len(player.started) == 1

    async def test_missing_saved_track_falls_back_to_current(self, state: BotState) -> None:
        state.current_track_id = "webdav_gone"
        state.playback_position_seconds = 99
        player = FakePlayer()
        prov = ScriptedProvider(
            current_seq=[make_track(title="NextPlayable")],
            by_id_seq=[ProviderError("GET /tracks/webdav_gone -> HTTP 404: unknown track")],
        )
        await _resume_or_start(player, prov, state, initial_backoff=0.001)  # type: ignore[arg-type]
        assert len(player.started) == 1
        assert player.started[0][0].title == "NextPlayable"
        assert player.started[0][1] == 0

    async def test_gives_up_after_max_attempts(self, state: BotState) -> None:
        player = FakePlayer()
        prov = ScriptedProvider([ProviderError("nope")] * 5)
        await _resume_or_start(
            player,
            prov,
            state,  # type: ignore[arg-type]
            max_attempts=3,
            initial_backoff=0.001,
            max_backoff=0.001,
        )
        assert player.started == []


class TestRecoverSilentPlayback:
    def _station(self, player: FakePlayer, *, connected: bool = True, listeners: int = 1):
        vc = type("VC", (), {"is_connected": lambda self: connected})()
        return type(
            "S",
            (),
            {
                "listener_count": listeners,
                "player": player,
                "guild_id": "g",
                "voice_client": vc,
                "is_paused": False,
            },
        )()

    async def test_restarts_when_occupied_and_silent(self, state: BotState) -> None:
        player = FakePlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        prov = ScriptedProvider([make_track(title="Recovered")])
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=0.0,
        )
        assert ok is True
        assert player.started[0][0].title == "Recovered"

    async def test_waits_for_the_silence_gate(self, state: BotState) -> None:
        """First silent sample must not fire recovery (2026-10-04 storm)."""
        player = FakePlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        prov = ScriptedProvider([make_track(title="Recovered")])
        kw = dict(admin_paused=False, silence_gate=5.0)  # type: ignore[arg-type]
        # t=0: gate opens the timer, no restart.
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=100.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        assert player.started == []
        # t=4: still inside the gate.
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=104.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        assert player.started == []
        # t=5: gate elapsed — recover.
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=105.0,
                **kw,  # type: ignore[arg-type]
            )
            is True
        )
        assert player.started[0][0].title == "Recovered"

    async def test_gate_timer_arms_while_voice_is_down(self, state: BotState) -> None:
        """A dropped voice link must not restart the silence clock."""
        player = FakePlayer(playing=False)
        station = self._station(player, connected=False)
        radio = RadioClock()
        prov = ScriptedProvider([make_track()])
        kw = dict(admin_paused=False, silence_gate=5.0)  # type: ignore[arg-type]
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=100.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        # Voice came back at t=105; the outage already lasted 5s since t=100.
        station.voice_client = type("VC", (), {"is_connected": lambda self: True})()
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=105.0,
                **kw,  # type: ignore[arg-type]
            )
            is True
        )
        assert len(player.started) == 1

    async def test_resumes_at_shared_position(self, state: BotState) -> None:
        """Cursor must be preserved — never slammed back to 0 on recovery."""
        player = FakePlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        radio.start(123.0)
        prov = ScriptedProvider([make_track(title="Recovered", duration_seconds=300)])
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=0.0,
            now=100.0,
        )
        assert ok is True
        assert player.started[0][1] == pytest.approx(123.0, abs=1.0)
        assert radio.position() == pytest.approx(123.0, abs=1.0)
        assert state.playback_position_seconds == 123

    async def test_advances_when_cursor_is_past_track_end(self, state: BotState) -> None:
        player = FakePlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        radio.start(500.0)
        prov = ScriptedProvider(
            [make_track(title="Finished", duration_seconds=300)],
            next_seq=[make_track(title="Next", duration_seconds=300, track_id="t2")],
        )
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=0.0,
            now=100.0,
        )
        assert ok is True
        assert player.started[0][0].title == "Next"
        assert player.started[0][1] == 0.0

    async def test_clears_silence_marker_when_playing(self, state: BotState) -> None:
        """Audible again → next outage is timed from its own first sample."""
        player = FakePlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        prov = ScriptedProvider([make_track(title="A"), make_track(title="B")])
        kw = dict(admin_paused=False, silence_gate=5.0)  # type: ignore[arg-type]
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=100.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        player._playing = True
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=104.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        player._playing = False
        # Marker cleared → a fresh 5s gate, not the stale one from t=100.
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=105.0,
                **kw,  # type: ignore[arg-type]
            )
            is False
        )
        assert (
            await recover_silent_playback(
                {"g": station},
                prov,
                radio,
                state,
                now=110.0,
                **kw,  # type: ignore[arg-type]
            )
            is True
        )
        assert player.started[0][0].title == "A"  # first provider.current() wins

    async def test_gives_up_on_position_when_still_silent(self, state: BotState) -> None:
        """A restart at the shared offset that yields no sound falls back to 0."""
        player = NeverPlayingPlayer(playing=False)
        station = self._station(player)
        radio = RadioClock()
        radio.start(123.0)
        prov = ScriptedProvider([make_track(title="A"), make_track(title="A")])
        kw = dict(admin_paused=False, silence_gate=5.0)  # type: ignore[arg-type]
        # t=100: first sample, gate opens.
        assert await recover_silent_playback(
            {"g": station}, prov, radio, state, now=100.0, **kw  # type: ignore[arg-type]
        ) is False
        # t=106: gate passed, but still inside the give-up window -> resume at 123.
        assert await recover_silent_playback(
            {"g": station}, prov, radio, state, now=106.0, **kw  # type: ignore[arg-type]
        ) is True
        assert player.started[-1][1] == pytest.approx(123.0, abs=1.0)
        # Still silent at t=108 (8s) -> restart the same track from the top.
        assert await recover_silent_playback(
            {"g": station}, prov, radio, state, now=108.0, **kw  # type: ignore[arg-type]
        ) is True
        assert player.started[-1][1] == 0.0

    async def test_noop_when_voice_client_disconnected(self, state: BotState) -> None:
        player = FakePlayer(playing=False)
        station = self._station(player, connected=False)
        radio = RadioClock()
        prov = ScriptedProvider([make_track(title="Recovered")])
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=5.0,
            now=1000.0,
        )
        assert ok is False
        assert player.started == []

    async def test_noop_when_already_playing(self, state: BotState) -> None:
        player = FakePlayer(playing=True)
        station = self._station(player)
        radio = RadioClock()
        prov = ScriptedProvider([make_track()])
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=0.0,
        )
        assert ok is False
        assert player.started == []

    async def test_noop_when_empty(self, state: BotState) -> None:
        player = FakePlayer(playing=False)
        station = self._station(player, listeners=0)
        radio = RadioClock()
        prov = ScriptedProvider([make_track()])
        ok = await recover_silent_playback(
            {"g": station},  # type: ignore[arg-type]
            prov,
            radio,
            state,
            admin_paused=False,
            silence_gate=0.0,
        )
        assert ok is False


class TestEnsureStationVoiceConnected:
    async def test_already_connected_returns_true(self) -> None:
        vc = type("VC", (), {"is_connected": lambda self: True})()
        station = type("S", (), {"voice_client": vc, "guild_id": "1", "voice_channel_id": 10})()
        client = object()
        assert await ensure_station_voice_connected(client, station) is True  # type: ignore[arg-type]

    async def test_reconnects_when_disconnected(self) -> None:
        new_vc = type("VC", (), {"is_connected": lambda self: True})()

        class FakeChannel:
            async def connect(self, reconnect=True, timeout=30.0):
                return new_vc

        channel = FakeChannel()

        class FakeGuild:
            voice_client = None

            def get_channel(self, ch_id):
                return channel if ch_id == 10 else None

        guild = FakeGuild()

        class FakeClient:
            def get_guild(self, g_id):
                return guild if g_id == 1 else None

        old_disconnected_vc = type(
            "OldVC",
            (),
            {
                "is_connected": lambda self: False,
                "disconnect": lambda self, force=True: None,
            },
        )()

        player = type("P", (), {"voice_client": old_disconnected_vc})()
        station = type(
            "S",
            (),
            {
                "voice_client": old_disconnected_vc,
                "guild_id": "1",
                "voice_channel_id": 10,
                "voice_channel": None,
                "player": player,
            },
        )()

        client = FakeClient()
        ok = await ensure_station_voice_connected(client, station)  # type: ignore[arg-type]
        assert ok is True
        assert station.voice_client is new_vc
        assert station.voice_channel is channel
        assert player.voice_client is new_vc

    async def test_returns_false_when_channel_missing(self) -> None:
        class FakeGuild:
            voice_client = None

            def get_channel(self, ch_id):
                return None

        class FakeClient:
            def get_guild(self, g_id):
                return FakeGuild()

        station = type(
            "S",
            (),
            {
                "voice_client": None,
                "guild_id": "1",
                "voice_channel_id": 10,
                "voice_channel": None,
                "player": None,
            },
        )()

        ok = await ensure_station_voice_connected(FakeClient(), station)  # type: ignore[arg-type]
        assert ok is False


# --------------------------------------------------------- _non_bot_members ----
class TestNonBotMembers:
    """Cover the discord-cache-race workaround explicitly."""

    @dataclass
    class FakeM:
        id: int
        bot: bool = False

    @dataclass
    class FakeCh:
        members: list

    def test_filters_bots(self) -> None:
        from bot.main import _non_bot_members

        ch = self.FakeCh(members=[self.FakeM(1), self.FakeM(2, bot=True)])
        assert len(_non_bot_members(ch)) == 1

    def test_excludes_by_id(self) -> None:
        from bot.main import _non_bot_members

        ch = self.FakeCh(members=[self.FakeM(1), self.FakeM(2)])
        assert len(_non_bot_members(ch, exclude_user_id="1")) == 1
