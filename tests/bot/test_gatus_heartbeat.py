"""Tests for the Gatus voice-heartbeat module (bot.gatus_heartbeat).

Follows the repo's fake-based test style: fake stations / voice clients and a
duck-typed recording HTTP client, plus one respx integration test against a
real ``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest
import respx

from bot.gatus_heartbeat import (
    ENDPOINT_PATH,
    GatusHeartbeat,
    is_radio_healthy,
)

BASE = "https://gatus.example.com"
PUSH_URL = f"{BASE}{ENDPOINT_PATH}"


# ------------------------------------------------------------------ fakes
class FakeVoiceClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


@dataclass
class FakeStation:
    voice_client: FakeVoiceClient
    listener_count: int = 0


def make_stations(*connected: bool) -> dict[str, FakeStation]:
    return {f"g{i}": FakeStation(FakeVoiceClient(c), listener_count=1) for i, c in enumerate(connected)}


class RecordingClient:
    """Duck-typed httpx stand-in: records every POST."""

    def __init__(self, *, fail: bool = False, status: int = 200) -> None:
        self.posts: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.fail = fail
        self.status = status

    async def post(
        self, url: str, *, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response:
        self.posts.append((url, params, headers))
        if self.fail:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(self.status, request=httpx.Request("POST", url))


def make_heartbeat(**kw) -> GatusHeartbeat:
    kw.setdefault("push_url", BASE)
    kw.setdefault("push_token", "tok")
    return GatusHeartbeat(**kw)


# ------------------------------------------------------------ is_radio_healthy
class TestIsRadioHealthy:
    def test_empty_stations_is_healthy(self) -> None:
        # No listeners anywhere — silence is intentional (pause when empty).
        assert is_radio_healthy({}) is True

    def test_connected_station_is_healthy(self) -> None:
        assert is_radio_healthy(make_stations(True)) is True

    def test_disconnected_station_is_unhealthy(self) -> None:
        # Voice dropped while someone is listening.
        assert is_radio_healthy(make_stations(False)) is False

    def test_disconnected_empty_station_doesnt_fail(self) -> None:
        # A disconnected station with no listeners must not fail health when
        # another station is connected and has listeners.
        stations = {
            **make_stations(False),
            "g1": FakeStation(FakeVoiceClient(True), listener_count=1),
        }
        stations["g0"].listener_count = 0
        assert is_radio_healthy(stations) is True

    def test_all_disconnected_is_unhealthy(self) -> None:
        assert is_radio_healthy(make_stations(False, False)) is False


# ---------------------------------------------------------------------- push
class TestPush:
    async def test_pushes_success_with_bearer_header(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        ok = await hb.push(client, success=True)
        assert ok is True
        url, params, headers = client.posts[0]
        assert url == PUSH_URL
        assert params == {"success": "true"}
        assert headers == {"Authorization": "Bearer tok"}

    async def test_pushes_failure_with_error_text(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        await hb.push(client, success=False, error="voice disconnected")
        _, params, _ = client.posts[0]
        assert params["success"] == "false"
        assert params["error"] == "voice disconnected"

    async def test_non_2xx_is_a_failed_push(self, caplog: pytest.LogCaptureFixture) -> None:
        hb = make_heartbeat()
        client = RecordingClient(status=500)
        ok = await hb.push(client, success=True)
        assert ok is False
        assert "gatus heartbeat push failed" in caplog.text

    async def test_network_error_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        hb = make_heartbeat()
        client = RecordingClient(fail=True)
        ok = await hb.push(client, success=True)
        assert ok is False
        assert "gatus heartbeat push failed" in caplog.text

    async def test_trailing_slash_on_base_is_tolerated(self) -> None:
        hb = make_heartbeat(push_url=f"{BASE}/")
        client = RecordingClient()
        await hb.push(client, success=True)
        assert client.posts[0][0] == PUSH_URL


# ---------------------------------------------------------------------- tick
class TestTick:
    async def test_healthy_tick_pushes_success_every_time(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        await hb.tick(client, is_healthy=True)
        await hb.tick(client, is_healthy=True)
        assert [p[1]["success"] for p in client.posts] == ["true", "true"]

    async def test_healthy_to_unhealthy_pushes_failure_once(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        await hb.tick(client, is_healthy=True)
        await hb.tick(client, is_healthy=False)
        await hb.tick(client, is_healthy=False)
        await hb.tick(client, is_healthy=False)
        assert [p[1]["success"] for p in client.posts] == ["true", "false"]
        assert client.posts[1][1]["error"] == "voice disconnected"

    async def test_unhealthy_to_healthy_pushes_success_immediately(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        await hb.tick(client, is_healthy=False)
        await hb.tick(client, is_healthy=True)
        await hb.tick(client, is_healthy=True)
        assert [p[1]["success"] for p in client.posts] == ["false", "true", "true"]

    async def test_first_tick_unhealthy_alerts(self) -> None:
        hb = make_heartbeat()
        client = RecordingClient()
        await hb.tick(client, is_healthy=False)
        assert [p[1]["success"] for p in client.posts] == ["false"]

    async def test_unhealthy_transition_pushes_failure_once_even_if_push_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed failure-push must not spam: stay silent until recovery."""
        hb = make_heartbeat()
        client = RecordingClient(fail=True)
        await hb.tick(client, is_healthy=True)
        await hb.tick(client, is_healthy=False)
        await hb.tick(client, is_healthy=False)
        assert len(client.posts) == 2  # success + one (failed) failure push
        assert "gatus heartbeat push failed" in caplog.text


# ---------------------------------------------------------------------- run
class TestRunLoop:
    async def test_cadence_and_transitions(self) -> None:
        hb = make_heartbeat(interval_seconds=0.01)
        client = RecordingClient()
        healthy = {"ok": True}

        def check() -> bool:
            return healthy["ok"]

        task = asyncio.create_task(hb.run(client, health_check=check))
        await asyncio.sleep(0.05)  # healthy: several success pushes
        healthy["ok"] = False
        await asyncio.sleep(0.05)  # unhealthy: one failure push, then silent
        healthy["ok"] = True
        await asyncio.sleep(0.05)  # recovery: success cadence resumes
        task.cancel()
        await task

        successes = [p[1]["success"] for p in client.posts]
        assert successes.count("false") == 1
        assert successes[0] == "true"
        assert successes[-1] == "true"
        assert successes.count("true") >= 4

    async def test_cancellation_stops_the_loop(self) -> None:
        hb = make_heartbeat(interval_seconds=0.01)
        client = RecordingClient()
        task = asyncio.create_task(hb.run(client, health_check=lambda: True))
        await asyncio.sleep(0.05)
        task.cancel()
        await task  # must not raise — loop swallows CancelledError
        count = len(client.posts)
        await asyncio.sleep(0.05)
        assert len(client.posts) == count  # no more pushes after cancel


# ------------------------------------------------------------ respx integration
class TestRespxIntegration:
    @respx.mock
    async def test_real_httpx_client_pushes_correct_request(self) -> None:
        route = respx.post(PUSH_URL).mock(return_value=httpx.Response(200))
        hb = make_heartbeat(push_token="s3cret")
        async with httpx.AsyncClient() as client:
            ok = await hb.push(client, success=True)
        assert ok is True
        assert route.called
        req = route.calls.last.request
        assert req.headers["Authorization"] == "Bearer s3cret"
        assert req.url.params["success"] == "true"
