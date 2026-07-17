"""RadioClock + shared-cursor resume behaviour (review fix #1)."""

from __future__ import annotations

import time

from bot.main import RadioClock, resume_station_at_radio_position, sync_radio_state
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
        self.is_paused = False
        self.playback_position_seconds = 0


class FakeStation:
    """Minimal stand-in: sync_radio_state only reads listener_count."""

    def __init__(self, listener_count: int) -> None:
        self.listener_count = listener_count
        self.player = FakePlayer()


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


async def test_dashboard_pause_freezes_clock_and_resume_keeps_position(monkeypatch) -> None:
    """Regression (review #2): an admin (dashboard) pause must freeze the
    shared ``RadioClock``, and the subsequent dashboard resume must re-join
    stations at the pause offset — not at the later wall-clock position (which
    would silently skip audio).

    Scenario: radio playing for 30s -> admin pause -> 5 min wall-clock passes
    -> admin resume. The resumed seek must be ~30s, not ~330s.
    """
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])

    radio = RadioClock()
    radio.init_from_state(0.0, playing=True)
    seq[0] = 1030.0  # radio has been playing for 30s
    assert radio.position() == 30.0

    state = FakeState("t1")

    # One server with a listener present.
    stations = {"g1": FakeStation(listener_count=1)}

    # --- Admin presses Pause ---
    admin_paused = True
    sync_radio_state(stations, radio, state, admin_paused=admin_paused)
    assert state.is_paused is True

    # Five minutes of wall-clock time pass while paused.
    seq[0] = 1330.0
    # The clock must stay frozen at 30s, NOT advance to 330s.
    assert radio.position() == 30.0

    # --- Admin presses Resume ---
    admin_paused = False
    sync_radio_state(stations, radio, state, admin_paused=admin_paused)
    assert state.is_paused is False

    # The resume command re-joins the live station at radio.position().
    track = await resume_station_at_radio_position(
        stations["g1"].player, FakeProvider(_track()), state, radio
    )
    assert track is not None
    # Critical: seek equals the pause offset (30), not the wall-clock (330).
    assert stations["g1"].player.starts == [(track, 30.0)]


def test_radio_clock_reset_is_frozen(monkeypatch) -> None:
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])
    rc = RadioClock()
    rc.start(50.0)
    seq[0] = 1010.0  # 10s elapsed -> 50 + 10
    assert rc.position() == 60.0

    # reset parks the cursor at a frozen offset without starting the clock.
    rc.reset(0)
    assert rc.is_playing() is False
    seq[0] = 1660.0  # a minute passes
    assert rc.position() == 0.0  # still frozen at 0


async def test_play_track_with_no_listeners_freezes_clock_at_zero(monkeypatch) -> None:
    """Regression (2nd review, high): selecting a track from the dashboard
    while nobody is listening must NOT start the shared RadioClock. The clock
    must stay frozen at offset 0 until a listener joins — otherwise the first
    joiner would start mid-track, skipping the opening minutes nobody heard.

    Scenario: 0 listeners -> play_track -> 10 min wall-clock passes -> first
    listener joins. The joiner must start at 0, not 600s in.
    """
    seq = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: seq[0])

    radio = RadioClock()
    radio.init_from_state(0.0, playing=False)  # currently paused
    state = FakeState("t1")

    # No servers have listeners yet.
    stations = {"g1": FakeStation(listener_count=0)}

    # Dashboard "Play now": reset cursor to 0, then reconcile radio state.
    admin_paused = False
    radio.reset(0)
    state.playback_position_seconds = 0
    sync_radio_state(stations, radio, state, admin_paused=admin_paused)

    # With no listeners, the clock must remain frozen at 0.
    assert radio.is_playing() is False
    assert radio.position() == 0.0

    # Ten minutes of wall-clock time pass with nobody listening.
    seq[0] = 1600.0
    # The clock must NOT have advanced to 600s.
    assert radio.position() == 0.0

    # First listener joins: they must start at offset 0, not 600s in.
    stations["g1"].listener_count = 1
    track = await resume_station_at_radio_position(
        stations["g1"].player, FakeProvider(_track()), state, radio
    )
    assert track is not None
    assert stations["g1"].player.starts == [(track, 0.0)]
