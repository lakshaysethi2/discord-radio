"""Pure helpers for voice-channel presence logic.

Extracted from `bot.main` so that the "should we pause? should we resume?"
decisions can be unit-tested without discord.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Transition(Enum):
    JOINED = "joined"
    LEFT = "left"
    IRRELEVANT = "irrelevant"  # move within guild, wrong channel, etc.


@dataclass(slots=True, frozen=True)
class VoiceEvent:
    """Simplified voice-state-update, discord.py-agnostic."""

    user_id: str
    is_bot: bool
    before_channel_id: int | None
    after_channel_id: int | None

    def transition(self, target_channel_id: int) -> Transition:
        was = self.before_channel_id == target_channel_id
        is_now = self.after_channel_id == target_channel_id
        if not was and is_now:
            return Transition.JOINED
        if was and not is_now:
            return Transition.LEFT
        return Transition.IRRELEVANT


def should_pause(remaining_non_bot_count: int, currently_paused: bool) -> bool:
    """After a user leaves, decide whether to pause playback."""
    return remaining_non_bot_count == 0 and not currently_paused


def should_resume(non_bot_count_after_join: int, currently_paused: bool) -> bool:
    """After a user joins, decide whether to resume playback."""
    return non_bot_count_after_join == 1 and currently_paused
