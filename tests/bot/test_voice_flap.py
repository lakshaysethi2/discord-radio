"""Tests for the voice flap detector (issue #27).

Simulates the Discord-kick loop: repeated 4014 disconnects must trip the flap
detector, engage exponential backoff, bump the cycle metric, and switch the
reconnect path to a fresh handshake (reconnect=False).
"""

from __future__ import annotations

import logging

from bot.gatus_heartbeat import is_radio_healthy
from bot.main import ensure_station_voice_connected, handle_bot_voice_disconnect
from bot.voice_flap import (
    VoiceFlapTracker,
    backoff_for,
    close_code_str,
    extract_close_code,
    is_server_kick,
)


def make_station(guild_id: str = "1", voice_client=None):  # type: ignore[no-untyped-def]
    player = type("P", (), {"voice_client": voice_client})()
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


class TestFlapTracker:
    def test_no_flap_below_threshold(self) -> None:
        trk = VoiceFlapTracker()
        d1 = trk.record_disconnect("g", close_code=4014, now=1000.0)
        d2 = trk.record_disconnect("g", close_code=4014, now=1030.0)
        assert not d1.is_flap and not d2.is_flap
        assert not trk.is_flapping("g", now=1030.0)
        assert trk.backoff_for_guild("g", now=1030.0) == 0.0

    def test_flap_at_threshold_with_backoff(self) -> None:
        trk = VoiceFlapTracker()
        trk.record_disconnect("g", now=1000.0)
        trk.record_disconnect("g", now=1030.0)
        d = trk.record_disconnect("g", close_code=4022, now=1060.0)
        assert d.is_flap is True
        assert d.kick_count == 3
        assert d.backoff_seconds == 2.0
        assert trk.is_flapping("g", now=1060.0)

    def test_backoff_doubles_then_caps(self) -> None:
        assert backoff_for(3) == 2.0
        assert backoff_for(4) == 4.0
        assert backoff_for(5) == 8.0
        assert backoff_for(100) == 60.0

    def test_backoff_uses_custom_threshold(self) -> None:
        assert backoff_for(2, threshold=2) == 2.0
        assert backoff_for(3, threshold=2) == 4.0
        trk = VoiceFlapTracker(threshold=5)
        for _ in range(4):
            d = trk.record_disconnect("g", close_code=4014, now=1000.0)
        assert not d.is_flap
        assert trk.backoff_for_guild("g", now=1000.0) == 0.0
        d = trk.record_disconnect("g", close_code=4014, now=1000.0)
        assert d.is_flap and d.backoff_seconds == 2.0

    def test_clean_close_code_counts_metric_not_flap(self) -> None:
        assert is_server_kick(4014) is True
        assert is_server_kick(4022) is True
        assert is_server_kick(None) is True  # code usually invisible; still counts
        assert is_server_kick(1000) is False
        trk = VoiceFlapTracker()
        for _ in range(5):
            d = trk.record_disconnect("g", close_code=1000, now=1000.0)
        assert not d.is_flap  # known-clean drops never trip a full reset
        assert trk.voice_cycles_total("g") == 5  # ...but the metric still bumps
        assert trk.cycles_snapshot() == {"g": 5}

    def test_old_kicks_expire(self) -> None:
        trk = VoiceFlapTracker()
        trk.record_disconnect("g", now=1000.0)
        trk.record_disconnect("g", now=1010.0)
        assert trk.kick_count("g", now=1300.1) == 1  # only the 1010 kick still in-window
        assert trk.kick_count("g", now=1310.1) == 0  # window (300s) fully elapsed
        assert not trk.is_flapping("g", now=2000.0)

    def test_metric_counts_every_cycle(self) -> None:
        trk = VoiceFlapTracker()
        for i in range(5):
            trk.record_disconnect("g", now=1000.0 + i * 10)
        assert trk.voice_cycles_total("g") == 5
        assert trk.voice_cycles_total("other") == 0

    def test_guilds_are_independent(self) -> None:
        trk = VoiceFlapTracker()
        for _ in range(3):
            trk.record_disconnect("a", now=1000.0)
        assert trk.is_flapping("a", now=1000.0)
        assert not trk.is_flapping("b", now=1000.0)


class TestCloseCode:
    def test_str_never_blank(self) -> None:
        assert close_code_str(4014) == "4014"
        assert close_code_str(None) == "unknown"

    def test_extract_from_ws(self) -> None:
        ws = type("WS", (), {"close_code": 4014})()
        assert extract_close_code(type("VC", (), {"ws": ws})()) == 4014

    def test_extract_missing_is_none(self) -> None:
        assert extract_close_code(object()) is None
        assert extract_close_code(None) is None


class TestHandleBotVoiceDisconnect:
    def test_logs_close_code_and_cycle(self, caplog) -> None:  # type: ignore[no-untyped-def]
        vc = type("VC", (), {"is_connected": lambda self: False})()
        station = make_station(voice_client=vc)
        trk = VoiceFlapTracker()
        with caplog.at_level(logging.WARNING, logger="bot.main"):
            decision = handle_bot_voice_disconnect(station, tracker=trk, close_code=4014)
        assert not decision.is_flap
        assert "close_code=4014" in caplog.text
        assert "cycle #1" in caplog.text
        assert station.voice_client is None
        assert station.player.voice_client is None  # player kept in sync
        assert trk.voice_cycles_total("1") == 1

    def test_flap_logged_on_third_kick(self, caplog) -> None:  # type: ignore[no-untyped-def]
        station = make_station()
        trk = VoiceFlapTracker()
        with caplog.at_level(logging.WARNING, logger="bot.main"):
            handle_bot_voice_disconnect(station, tracker=trk, close_code=4014)
            station.voice_client = object()
            handle_bot_voice_disconnect(station, tracker=trk, close_code=4014)
            station.voice_client = object()
            decision = handle_bot_voice_disconnect(station, tracker=trk, close_code=4022)
        assert decision.is_flap is True
        assert "FLAP" in caplog.text
        assert "close_code=4022" in caplog.text


class TestEnsureFullReset:
    def _client(self, channel):  # type: ignore[no-untyped-def]
        class FakeGuild:
            voice_client = None

            def get_channel(self, ch_id):  # type: ignore[no-untyped-def]
                return channel if ch_id == 10 else None

        guild = FakeGuild()

        class FakeClient:
            def get_guild(self, g_id):  # type: ignore[no-untyped-def]
                return guild if g_id == 1 else None

        return FakeClient()

    async def test_normal_path_uses_resume(self) -> None:
        calls: dict = {}
        sleeps: list[float] = []

        class FakeChannel:
            async def connect(self, reconnect=True, timeout=30.0):  # type: ignore[no-untyped-def]
                calls["reconnect"] = reconnect
                return type("VC", (), {"is_connected": lambda self: True})()

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)

        station = make_station()
        ok = await ensure_station_voice_connected(
            self._client(FakeChannel()),
            station,  # type: ignore[arg-type]
            flap=VoiceFlapTracker(),
            sleep=fake_sleep,
        )
        assert ok is True
        assert calls["reconnect"] is True
        assert sleeps == []

    async def test_flap_path_fresh_handshake_with_backoff(self) -> None:
        calls: dict = {}
        sleeps: list[float] = []

        class FakeChannel:
            async def connect(self, reconnect=True, timeout=30.0):  # type: ignore[no-untyped-def]
                calls["reconnect"] = reconnect
                return type("VC", (), {"is_connected": lambda self: True})()

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)

        trk = VoiceFlapTracker()
        for _ in range(3):
            trk.record_disconnect("1", close_code=4014)
        station = make_station()
        ok = await ensure_station_voice_connected(
            self._client(FakeChannel()),
            station,  # type: ignore[arg-type]
            flap=trk,
            sleep=fake_sleep,
        )
        assert ok is True
        assert calls["reconnect"] is False  # fresh handshake, no resume
        assert sleeps == [2.0]  # backoff engaged


class TestGatusFlapAlert:
    def _station(self, guild_id="g", listeners=1, playing=True):  # type: ignore[no-untyped-def]
        player = type("P", (), {"is_playing": lambda self: playing})()
        vc = type("VC", (), {"is_connected": lambda self: True})()
        return type(
            "S",
            (),
            {
                "guild_id": guild_id,
                "listener_count": listeners,
                "voice_client": vc,
                "player": player,
                "is_paused": False,
            },
        )()

    def test_flapping_station_is_unhealthy(self) -> None:
        trk = VoiceFlapTracker()
        for _ in range(3):
            trk.record_disconnect("g", close_code=4014, now=1000.0)
        stations = {"g": self._station()}
        assert is_radio_healthy(stations, now=1060.0, flap_tracker=trk) is False

    def test_calm_station_is_healthy(self) -> None:
        stations = {"g": self._station()}
        assert is_radio_healthy(stations, flap_tracker=VoiceFlapTracker()) is True

    def test_no_listeners_healthy_despite_flap(self) -> None:
        trk = VoiceFlapTracker()
        for _ in range(3):
            trk.record_disconnect("g", close_code=4014, now=1000.0)
        stations = {"g": self._station(listeners=0, playing=False)}
        assert is_radio_healthy(stations, now=1060.0, flap_tracker=trk) is True
