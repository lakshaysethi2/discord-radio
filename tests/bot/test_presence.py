from __future__ import annotations

from bot.presence import Transition, VoiceEvent, should_pause, should_resume


class TestTransition:
    def test_joined_target(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=None, after_channel_id=42)
        assert e.transition(42) is Transition.JOINED

    def test_left_target(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=42, after_channel_id=None)
        assert e.transition(42) is Transition.LEFT

    def test_moved_within_target(self) -> None:
        # (e.g. mute toggled — same channel before + after)
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=42, after_channel_id=42)
        assert e.transition(42) is Transition.IRRELEVANT

    def test_moved_between_other_channels(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=1, after_channel_id=2)
        assert e.transition(42) is Transition.IRRELEVANT

    def test_joined_wrong_channel(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=None, after_channel_id=99)
        assert e.transition(42) is Transition.IRRELEVANT

    def test_left_wrong_channel(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=99, after_channel_id=None)
        assert e.transition(42) is Transition.IRRELEVANT

    def test_moved_from_target_to_other(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=42, after_channel_id=99)
        assert e.transition(42) is Transition.LEFT

    def test_moved_from_other_to_target(self) -> None:
        e = VoiceEvent(user_id="u", is_bot=False, before_channel_id=99, after_channel_id=42)
        assert e.transition(42) is Transition.JOINED


class TestShouldPause:
    def test_pause_when_now_empty_and_playing(self) -> None:
        assert should_pause(remaining_non_bot_count=0, currently_paused=False) is True

    def test_no_pause_if_already_paused(self) -> None:
        assert should_pause(remaining_non_bot_count=0, currently_paused=True) is False

    def test_no_pause_if_still_someone(self) -> None:
        assert should_pause(remaining_non_bot_count=1, currently_paused=False) is False


class TestShouldResume:
    def test_resume_on_first_joiner(self) -> None:
        assert should_resume(non_bot_count_after_join=1, currently_paused=True) is True

    def test_no_resume_if_not_paused(self) -> None:
        assert should_resume(non_bot_count_after_join=1, currently_paused=False) is False

    def test_no_resume_if_others_already_there(self) -> None:
        # Someone else joins while another user is already present — playback
        # was already running, no need to resume.
        assert should_resume(non_bot_count_after_join=2, currently_paused=True) is False
