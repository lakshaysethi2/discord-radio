"""Typed adapter over the `bot_state` table.

The bot_state table is a key/value store used to remember playback position,
current track, message ids, and the last monthly-reset month. This module
adds a small typed layer so bot code doesn't sprinkle stringly-typed calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from db.database import Database
from db.models import BOT_STATE_KEYS, BotStateKey


@dataclass(slots=True)
class PlaybackSnapshot:
    """Everything needed to resume playback after a restart or pause."""

    current_track_id: str | None
    playback_position_seconds: int
    is_paused: bool
    now_playing_message_id: int | None
    playlist_position: int


class BotState:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------- raw kv API
    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in BOT_STATE_KEYS:
            # Guard against typos — every persistent key should be declared.
            raise KeyError(f"unknown bot_state key {key!r}")
        return self.db.get_state(key, default)

    def set(self, key: str, value: str | int | float | bool | None) -> None:
        if key not in BOT_STATE_KEYS:
            raise KeyError(f"unknown bot_state key {key!r}")
        self.db.set_state(key, value)

    # ------------------------------------------------- current track/position
    @property
    def current_track_id(self) -> str | None:
        v = self.get(BotStateKey.CURRENT_TRACK_ID)
        return v if v else None

    @current_track_id.setter
    def current_track_id(self, value: str | None) -> None:
        self.set(BotStateKey.CURRENT_TRACK_ID, value or "")

    @property
    def playback_position_seconds(self) -> int:
        return self.db.get_state_int(BotStateKey.PLAYBACK_POSITION_SECONDS, 0)

    @playback_position_seconds.setter
    def playback_position_seconds(self, value: int) -> None:
        self.set(BotStateKey.PLAYBACK_POSITION_SECONDS, max(0, int(value)))

    @property
    def is_paused(self) -> bool:
        return self.db.get_state_bool(BotStateKey.IS_PAUSED, False)

    @is_paused.setter
    def is_paused(self, value: bool) -> None:
        self.set(BotStateKey.IS_PAUSED, bool(value))

    @property
    def now_playing_message_id(self) -> int | None:
        v = self.get(BotStateKey.NOW_PLAYING_MESSAGE_ID)
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            return None

    @now_playing_message_id.setter
    def now_playing_message_id(self, value: int | None) -> None:
        self.set(BotStateKey.NOW_PLAYING_MESSAGE_ID, "" if value is None else int(value))

    @property
    def playlist_position(self) -> int:
        return self.db.get_state_int(BotStateKey.PLAYLIST_POSITION, 0)

    @playlist_position.setter
    def playlist_position(self, value: int) -> None:
        self.set(BotStateKey.PLAYLIST_POSITION, max(0, int(value)))

    @property
    def stream_volume_percent(self) -> int:
        """Persistent global FFmpeg gain, constrained to the admin UI range."""
        value = self.db.get_state_int(BotStateKey.STREAM_VOLUME_PERCENT, 100)
        return min(250, max(50, value))

    @stream_volume_percent.setter
    def stream_volume_percent(self, value: int) -> None:
        self.set(BotStateKey.STREAM_VOLUME_PERCENT, min(250, max(50, int(value))))

    @property
    def last_monthly_reset(self) -> str | None:
        v = self.get(BotStateKey.LAST_MONTHLY_RESET)
        return v if v else None

    @last_monthly_reset.setter
    def last_monthly_reset(self, value: str | None) -> None:
        self.set(BotStateKey.LAST_MONTHLY_RESET, value or "")

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(
            current_track_id=self.current_track_id,
            playback_position_seconds=self.playback_position_seconds,
            is_paused=self.is_paused,
            now_playing_message_id=self.now_playing_message_id,
            playlist_position=self.playlist_position,
        )
