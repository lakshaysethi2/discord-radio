"""Live-apply of a dashboard server save (§servers control plane).

Exercises `apply_server_config` — the idempotent reconcile that the bot runs
when it drains an `apply_server` command. The function is kept free of
discord.py I/O so it can be tested with fakes: `build_station` /
`teardown_station` are injected, and config comes from an injected
`get_guild_config`.
"""

from __future__ import annotations

from bot.main import apply_server_config
from db import guilds as guilds_db


class _FakeChannel:
    """Mimics `NowPlaying` / `MilestoneAnnouncer` text-channel repointing."""

    def __init__(self, text_channel_id: int) -> None:
        self.text_channel_id = text_channel_id


class FakeStation:
    def __init__(self, guild_id: str, voice_channel_id: str, text_channel_id: str) -> None:
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.now_playing = _FakeChannel(int(text_channel_id))
        self.milestones = _FakeChannel(int(text_channel_id))


class FakeGuild:
    def __init__(self, guild_id: str) -> None:
        self.id = int(guild_id)

    def get_channel(self, _cid):  # pragma: no cover — not used in apply path
        return None


def _cfg(guild_id: str, *, enabled: bool = True, vid="100", tid="200"):
    # Real guild_configs store numeric channel ids as strings.
    return guilds_db.GuildConfig(
        guild_id=guild_id,
        guild_name="Server",
        enabled=enabled,
        voice_channel_id=vid,
        text_channel_id=tid,
    )


class _Harness:
    """Wires fakes around `apply_server_config` and records callbacks."""

    def __init__(
        self,
        *,
        cfg=None,
        guild=None,
        build_returns=None,
        build_raises=False,
        build_fails=False,
    ) -> None:
        self.stations: dict[str, FakeStation] = {}
        self.announcers: dict[str, _FakeChannel] = {}
        self.built: list[tuple[object, object]] = []
        self.torn_down: list[FakeStation] = []
        self._cfg = cfg
        self._guild = guild
        self._build_returns = build_returns
        self._build_raises = build_raises
        self._build_fails = build_fails

    async def build_station(self, guild, cfg) -> FakeStation | None:
        self.built.append((guild, cfg))
        if self._build_raises:
            raise RuntimeError("boom")
        if self._build_fails:
            return None
        st = self._build_returns
        if isinstance(st, FakeStation):
            return st
        # Default: build a fresh station from the config.
        return FakeStation(cfg.guild_id, cfg.voice_channel_id, cfg.text_channel_id)

    async def teardown_station(self, station: FakeStation) -> None:
        self.torn_down.append(station)

    def get_guild_config(self, db, guild_id: str):
        return self._cfg

    def get_guild(self, guild_id: int):
        return self._guild

    async def run(self, guild_id: str, *, db=None) -> str:
        return await apply_server_config(
            db=db,
            client=self,
            stations=self.stations,
            per_guild_announcers=self.announcers,
            build_station=self.build_station,
            teardown_station=self.teardown_station,
            guild_id=guild_id,
            get_guild_config=self.get_guild_config,
        )


class TestApplyServerConfig:
    async def test_enable_builds_and_registers(self) -> None:
        h = _Harness(cfg=_cfg("1"), guild=FakeGuild("1"))
        res = await h.run("1")
        assert res == "ok:applied"
        assert "1" in h.stations
        assert "1" in h.announcers
        # The announcer registered is the station's own milestone announcer.
        assert h.announcers["1"] is h.stations["1"].milestones

    async def test_disable_tears_down_live_station(self) -> None:
        h = _Harness(cfg=_cfg("1", enabled=False))
        h.stations["1"] = FakeStation("1", "100", "200")
        h.announcers["1"] = h.stations["1"].milestones
        res = await h.run("1")
        assert res == "ok:disabled"
        assert "1" not in h.stations
        assert "1" not in h.announcers
        assert len(h.torn_down) == 1

    async def test_disable_when_nothing_live_is_noop(self) -> None:
        h = _Harness(cfg=_cfg("1", enabled=False))
        res = await h.run("1")
        assert res == "ok:disabled"
        assert h.torn_down == []

    async def test_voice_channel_change_rebuilds(self) -> None:
        h = _Harness(cfg=_cfg("1", vid="300", tid="200"), guild=FakeGuild("1"))
        h.stations["1"] = FakeStation("1", "100", "200")
        h.announcers["1"] = h.stations["1"].milestones
        res = await h.run("1")
        assert res == "ok:applied"
        # Old station torn down, new one built + registered.
        assert len(h.torn_down) == 1
        assert len(h.built) == 1
        assert h.stations["1"].voice_channel_id == "300"

    async def test_text_channel_change_repoints_in_place(self) -> None:
        h = _Harness(cfg=_cfg("1", vid="100", tid="210"))
        st = FakeStation("1", "100", "200")
        h.stations["1"] = st
        h.announcers["1"] = st.milestones
        res = await h.run("1")
        assert res == "ok:text_channel_updated"
        # No reconnect — station kept, only the text channel repointed.
        assert h.torn_down == []
        assert h.built == []
        assert st.text_channel_id == 210
        assert st.now_playing.text_channel_id == 210
        assert st.milestones.text_channel_id == 210
        assert h.announcers["1"].text_channel_id == 210

    async def test_already_matching_is_idempotent(self) -> None:
        h = _Harness(cfg=_cfg("1", vid="100", tid="200"))
        st = FakeStation("1", "100", "200")
        h.stations["1"] = st
        h.announcers["1"] = st.milestones
        res = await h.run("1")
        assert res == "ok:applied"
        assert h.built == []
        assert h.torn_down == []
        # Same station object retained.
        assert h.stations["1"] is st

    async def test_guild_not_found_errors(self) -> None:
        h = _Harness(cfg=_cfg("1"), guild=None)
        res = await h.run("1")
        assert res.startswith("error: guild")
        assert h.built == []

    async def test_voice_connect_failure_errors(self) -> None:
        h = _Harness(cfg=_cfg("1"), guild=FakeGuild("1"), build_fails=True)
        res = await h.run("1")
        assert res == "error: voice connect failed"

    async def test_incomplete_config_is_treated_as_disabled(self) -> None:
        # enabled=True but no text channel → not "wants_on" → disabled-ish.
        h = _Harness(cfg=_cfg("1", tid=None))
        h.stations["1"] = FakeStation("1", "100", "200")
        h.announcers["1"] = h.stations["1"].milestones
        res = await h.run("1")
        assert res == "ok:disabled"
        assert "1" not in h.stations
