"""Regression coverage for the useful recovery changes formerly only on main."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.main import RadioClock, recover_silent_playback
from tests.bot.test_main_helpers import FakePlayer, make_track


def station(*, connected=True, playing=False, listeners=1):
    return SimpleNamespace(
        guild_id="1",
        listener_count=listeners,
        is_paused=True,
        player=FakePlayer(playing=playing),
        voice_client=SimpleNamespace(is_connected=lambda: connected),
    )


def provider(track=None):
    return SimpleNamespace(
        current=AsyncMock(return_value=track or make_track()),
        next=AsyncMock(return_value=make_track(track_id="t2", playlist_position=1)),
    )


async def recover(stations, prov, radio, state, *, now=0, paused=False):
    return await recover_silent_playback(
        stations,
        prov,
        radio,
        state,
        admin_paused=paused,
        silence_gate_seconds=5,
        now=now,
    )


async def test_one_playing_guild_does_not_hide_another_silent_guild(state):
    silent, healthy = station(), station(playing=True)
    stations = {"silent": silent, "healthy": healthy}
    radio = RadioClock()
    radio.reset(42)
    prov = provider()
    assert not await recover(stations, prov, radio, state)
    assert await recover(stations, prov, radio, state, now=5)
    assert silent.player.started[0][1] == 42
    assert healthy.player.started == []
    assert radio.position() >= 42


async def test_gate_includes_disconnected_time(state):
    st = station(connected=False)
    radio, prov = RadioClock(), provider()
    assert not await recover({"g": st}, prov, radio, state)
    prov.current.assert_not_awaited()
    st.voice_client.is_connected = lambda: True
    assert await recover({"g": st}, prov, radio, state, now=5)


@pytest.mark.parametrize("mode", ["past_end", "different_track", "wrap"])
async def test_advance_resets_shared_and_persisted_cursors(state, mode):
    st = station()
    radio = RadioClock()
    radio.reset(400)
    state.current_track_id = "old" if mode == "different_track" else "t1"
    prov = provider()
    if mode == "wrap":
        prov.next.return_value = make_track()
    assert not await recover({"g": st}, prov, radio, state)
    assert await recover({"g": st}, prov, radio, state, now=5)
    assert st.player.started[0][1] == 0
    assert radio.position() < 1
    assert state.playback_position_seconds == 0
    if mode == "different_track":
        prov.next.assert_not_awaited()
        assert state.current_track_id == "t1"
    else:
        prov.next.assert_awaited_once()
        assert state.current_track_id == ("t1" if mode == "wrap" else "t2")


async def test_track_change_reaches_healthy_guild_too(state):
    silent, healthy = station(), station(playing=True)
    stations = {"s": silent, "h": healthy}
    radio, prov = RadioClock(), provider()
    radio.reset(301)
    state.current_track_id = "t1"
    await recover(stations, prov, radio, state)
    assert await recover(stations, prov, radio, state, now=5)
    assert healthy.player.started[0][0].track_id == "t2"
    assert silent.player.started[0][0].track_id == "t2"


@pytest.mark.parametrize("failure", [RuntimeError("offline"), make_track(ready=False)])
async def test_failed_advance_does_not_reset_clock(state, failure):
    st = station()
    radio, prov = RadioClock(), provider()
    radio.reset(400)
    state.current_track_id = "t1"
    if isinstance(failure, Exception):
        prov.next.side_effect = failure
    else:
        prov.next.return_value = failure
    await recover({"g": st}, prov, radio, state)
    assert not await recover({"g": st}, prov, radio, state, now=5)
    assert radio.position() == 400
    assert state.current_track_id == "t1"
    assert st.player.started == []


async def test_failed_start_keeps_gate_armed_and_does_not_report_success(state):
    st = station()
    st.player.start = AsyncMock(side_effect=RuntimeError("no audio"))
    radio, prov = RadioClock(), provider()
    await recover({"g": st}, prov, radio, state)
    assert not await recover({"g": st}, prov, radio, state, now=5)
    assert st._silence_recovery_since == 0
    assert not await recover({"g": st}, prov, radio, state, now=6)
    assert st.player.start.await_count == 2


@pytest.mark.parametrize("pause", [True, False])
async def test_empty_or_admin_pause_clears_gate(state, pause):
    st = station()
    radio, prov = RadioClock(), provider()
    await recover({"g": st}, prov, radio, state)
    if not pause:
        st.listener_count = 0
    assert not await recover({"g": st}, prov, radio, state, now=10, paused=pause)
    prov.current.assert_not_awaited()
    st.listener_count = 1
    assert not await recover({"g": st}, prov, radio, state, now=11)
    assert await recover({"g": st}, prov, radio, state, now=16)


async def test_unknown_duration_preserves_cursor_instead_of_rewinding(state):
    st = station()
    radio = RadioClock()
    radio.reset(20000)
    prov = provider(make_track(duration_seconds=0, has_video=True))
    await recover({"g": st}, prov, radio, state)
    assert await recover({"g": st}, prov, radio, state, now=50)
    assert st.player.started[0][1] == 20000
    prov.next.assert_not_awaited()
