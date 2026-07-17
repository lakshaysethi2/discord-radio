"""RadioClock + shared-cursor resume behaviour (review fix #1)."""

from __future__ import annotations

import time

from bot.main import RadioClock, resume_station_at_radio_position
from provider.client import TrackResponse


class FakePlayer:
    def __init__(self) -> None:
        self.starts: list[tuple[object, float]] = []

    async def start(self, track, *, seek_seconds: float = 0.0) -> None:
        self.starts.append((track, seek_seconds))


class FakeProvider:
    def __init__(self, track: TrackResponse) -> None:
        self._track = track

    async def get_by_id(self, _tid: str) -> TrackResponse:
        return self._track


class FakeState:
    def __init__(self, track_id: str | None) -> None:
        self.current_track_id = track_id


def _track(track_id: str = "t1") -> TrackResponse:
    return TrackResponse(
        track_id=track_id,
        title="T",
        duration_seconds=600,
        local_path="/cache/t.mp3",
        provider_used="local",
        playlist_position=0,
        ready=True,
    )


def test_radio_clock_advances_and_freezes(monkeypatch) -> None:
    seq = [1000.0]

    def fake() -> float:
        return seq[0]

    monkeypatch.setattr(time, "monotonic", fake)

    rc = RadioClock()
    rc.init_from_state(0.0, playing=True)
    assert rc.position() == 0.0

    seq[0] = 1030.0  # 30s of playback
    assert rc.position() == 30.0

    rc.pause()
    seq[0] = 1090.0  # time passes while paused
    assert rc.position() == 30.0  # frozen

    rc.start(rc.position())  # resume from frozen position
    seq[0] = 1100.0
    assert rc.position() == 40.0


def test_radio_clock_init_from_paused_state(monkeypatch) -> None:
    seq = [500.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])
    rc = RadioClock()
    rc.init_from_state(123.0, playing=False)
    assert rc.position() == 123.0
    assert rc.is_playing() is False


async def test_newly_joined_guild_starts_at_shared_position(monkeypatch) -> None:
    """Review scenario: Guild A plays for N seconds, then Guild B's first
    listener joins. Guild B must start the same track at ~N seconds, not 0."""
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])

    radio = RadioClock()
    radio.init_from_state(0.0, playing=True)
    seq[0] = 1030.0  # Guild A has been playing for 30s

    player = FakePlayer()
    state = FakeState("t1")
    track = await resume_station_at_radio_position(player, FakeProvider(_track()), state, radio)

    assert track is not None
    # Guild B joins the radio at the shared offset (~30s), not from the start.
    assert player.starts == [(track, 30.0)]


async def test_resume_falls_back_when_track_unavailable(monkeypatch) -> None:
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])
    radio = RadioClock()
    radio.init_from_state(0.0, playing=True)
    seq[0] = 1010.0

    # Mark the track as not ready so the helper bails out safely.
    bad = _track()
    bad.ready = False

    class UnavailableProvider:
        async def get_by_id(self, _tid: str) -> TrackResponse:
            return bad

    player = FakePlayer()
    state = FakeState("t1")
    result = await resume_station_at_radio_position(player, UnavailableProvider(), state, radio)
    assert result is None
    assert player.starts == []
