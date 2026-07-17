"""Tests for the pure-ish helpers in bot.main that don't need discord.py."""

from __future__ import annotations

from dataclasses import dataclass

from bot.main import _resume_or_start
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
    def __init__(self) -> None:
        self.started: list[tuple[TrackResponse, float]] = []

    async def start(self, track: TrackResponse, *, seek_seconds: float = 0.0) -> None:
        self.started.append((track, seek_seconds))


class ScriptedProvider:
    """Provider that returns a series of responses (raises included) in order."""

    def __init__(self, current_seq: list, by_id_seq: list | None = None) -> None:
        self._current = list(current_seq)
        self._by_id = list(by_id_seq or [])

    async def current(self) -> TrackResponse:
        item = self._current.pop(0)
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
