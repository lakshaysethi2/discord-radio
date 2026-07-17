from __future__ import annotations

import pytest

from bot.state import BotState, PlaybackSnapshot


class TestGuardedKeys:
    def test_unknown_key_raises(self, state: BotState) -> None:
        with pytest.raises(KeyError):
            state.get("unknown_key")
        with pytest.raises(KeyError):
            state.set("unknown_key", "x")


class TestCurrentTrackId:
    def test_default_none(self, state: BotState) -> None:
        assert state.current_track_id is None

    def test_roundtrip(self, state: BotState) -> None:
        state.current_track_id = "abc"
        assert state.current_track_id == "abc"

    def test_clear_to_none(self, state: BotState) -> None:
        state.current_track_id = "abc"
        state.current_track_id = None
        assert state.current_track_id is None


class TestPositions:
    def test_position_default_zero(self, state: BotState) -> None:
        assert state.playback_position_seconds == 0

    def test_position_roundtrip(self, state: BotState) -> None:
        state.playback_position_seconds = 123
        assert state.playback_position_seconds == 123

    def test_position_clamped_non_negative(self, state: BotState) -> None:
        state.playback_position_seconds = -5
        assert state.playback_position_seconds == 0

    def test_playlist_position(self, state: BotState) -> None:
        state.playlist_position = 42
        assert state.playlist_position == 42


class TestFlags:
    def test_is_paused_default_false(self, state: BotState) -> None:
        assert state.is_paused is False

    def test_is_paused_roundtrip(self, state: BotState) -> None:
        state.is_paused = True
        assert state.is_paused is True
        state.is_paused = False
        assert state.is_paused is False


class TestMessageId:
    def test_default_none(self, state: BotState) -> None:
        assert state.now_playing_message_id is None

    def test_roundtrip(self, state: BotState) -> None:
        state.now_playing_message_id = 987654321
        assert state.now_playing_message_id == 987654321

    def test_clear(self, state: BotState) -> None:
        state.now_playing_message_id = 1
        state.now_playing_message_id = None
        assert state.now_playing_message_id is None


class TestMonthlyReset:
    def test_default_none(self, state: BotState) -> None:
        assert state.last_monthly_reset is None

    def test_roundtrip(self, state: BotState) -> None:
        state.last_monthly_reset = "2024-11"
        assert state.last_monthly_reset == "2024-11"


class TestSnapshot:
    def test_snapshot_captures_all(self, state: BotState) -> None:
        state.current_track_id = "t1"
        state.playback_position_seconds = 60
        state.is_paused = True
        state.now_playing_message_id = 42
        state.playlist_position = 3
        snap = state.snapshot()
        assert snap == PlaybackSnapshot(
            current_track_id="t1",
            playback_position_seconds=60,
            is_paused=True,
            now_playing_message_id=42,
            playlist_position=3,
        )


class TestStreamVolume:
    def test_default_and_clamped_roundtrip(self, state: BotState) -> None:
        assert state.stream_volume_percent == 100
        state.stream_volume_percent = 125
        assert state.stream_volume_percent == 125
        state.stream_volume_percent = 999
        assert state.stream_volume_percent == 250
        state.stream_volume_percent = 1
        assert state.stream_volume_percent == 50
