from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bot.milestones import MilestoneAnnouncer, MilestoneChecker
from db.database import Database


def _seed(db: Database, user_id: str, alltime_seconds: int, **flags: int) -> None:
    db.execute(
        "INSERT INTO user_totals(user_id, username, total_seconds_alltime, "
        "total_seconds_monthly, month_key, "
        "milestone_5h, milestone_10h, milestone_100h, milestone_1000h) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            f"User_{user_id}",
            alltime_seconds,
            0,
            "2024-11",
            flags.get("m5", 0),
            flags.get("m10", 0),
            flags.get("m100", 0),
            flags.get("m1000", 0),
        ),
    )


class TestChecker:
    def test_unknown_user_no_milestones(self, db: Database) -> None:
        c = MilestoneChecker(db)
        assert c.check_user("ghost") == []

    def test_under_threshold_nothing(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=4 * 3600)
        c = MilestoneChecker(db)
        assert c.check_user("u1") == []

    def test_exactly_5h_triggers(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [5]
        # Flag flipped so re-check is empty.
        assert c.check_user("u1") == []

    def test_multiple_at_once(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=100 * 3600 + 5)  # crosses 5, 10, 100
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert sorted(m.hours for m in got) == [5, 10, 100]

    def test_existing_flags_respected(self, db: Database) -> None:
        # Already given 5h milestone previously.
        _seed(db, "u1", alltime_seconds=15 * 3600, m5=1)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [10]

    def test_1000h(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=1000 * 3600, m5=1, m10=1, m100=1)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert [m.hours for m in got] == [1000]

    def test_username_included(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        c = MilestoneChecker(db)
        got = c.check_user("u1")
        assert got[0].username == "User_u1"
        assert got[0].user_id == "u1"


# ------------------------------------------------------------------ announcer
@dataclass
class FakeChannel:
    sent: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        self.sent.append(content)


@dataclass
class FakeClient:
    channels: dict = field(default_factory=dict)

    def get_channel(self, cid: int):
        return self.channels.get(cid)


class TestAnnouncer:
    async def test_no_milestones_no_send(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=3600)  # 1h — below 5h threshold
        channel = FakeChannel()
        client = FakeClient(channels={42: channel})
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )
        got = await ann.check_and_announce("u1")
        assert got == []
        assert channel.sent == []

    async def test_sends_on_milestone(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        channel = FakeChannel()
        client = FakeClient(channels={42: channel})
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )
        got = await ann.check_and_announce("u1")
        assert len(got) == 1
        assert len(channel.sent) == 1
        assert "5 hours" in channel.sent[0]
        assert "<@u1>" in channel.sent[0]

    async def test_multiple_milestones_multiple_sends(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=100 * 3600)
        channel = FakeChannel()
        client = FakeClient(channels={42: channel})
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )
        got = await ann.check_and_announce("u1")
        assert len(got) == 3  # 5, 10, 100
        assert len(channel.sent) == 3

    async def test_no_channel_still_returns_milestones(self, db: Database) -> None:
        """If the text channel isn't found we still flip the flags."""
        _seed(db, "u1", alltime_seconds=5 * 3600)
        client = FakeClient(channels={})  # nothing at 42
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )
        got = await ann.check_and_announce("u1")
        assert len(got) == 1
        # Flag flipped even though we couldn't send.
        row = db.fetchone("SELECT milestone_5h FROM user_totals WHERE user_id='u1'")
        assert row["milestone_5h"] == 1

    async def test_idempotent_after_flip(self, db: Database) -> None:
        _seed(db, "u1", alltime_seconds=5 * 3600)
        channel = FakeChannel()
        client = FakeClient(channels={42: channel})
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )
        await ann.check_and_announce("u1")
        await ann.check_and_announce("u1")  # second call should be silent
        assert len(channel.sent) == 1


# ------------------------------------------------------------------ now playing
class FakeEmbedField:
    def __init__(self, name: str, value: str, inline: bool = True) -> None:
        self.name = name
        self.value = value
        self.inline = inline


class FakeEmbed:
    def __init__(
        self, title: str, description: str, fields: list[FakeEmbedField] | None = None
    ) -> None:
        self.title = title
        self.description = description
        self.fields = fields or []

    def set_field_at(self, index: int, *, name: str, value: str, inline: bool = True) -> None:
        self.fields[index] = FakeEmbedField(name, value, inline)


class FakeMessage:
    def __init__(self, id: int, embeds: list[FakeEmbed]) -> None:
        self.id = id
        self.embeds = embeds
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, embed: FakeEmbed) -> None:
        self.edits.append({"embed": embed})

    async def delete(self) -> None:
        self.deleted = True


class FakeDiscordChannel:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.sent_embeds: list = []

    async def fetch_message(self, msg_id: int) -> FakeMessage | None:
        return self.messages.get(msg_id)

    async def send(self, embed: Any) -> FakeMessage:
        self.sent_embeds.append(embed)
        msg = FakeMessage(id=12345, embeds=[embed])
        self.messages[12345] = msg
        return msg


class FakeState:
    def __init__(self) -> None:
        self.now_playing_message_id: int | None = None


@dataclass
class DummyTrack:
    track_id: str = "t1"
    title: str = "Test Title"
    duration_seconds: int = 120
    playlist_position: int = 0


class TestNowPlaying:
    def test_fmt_duration(self, db: Database) -> None:
        from bot.milestones import NowPlaying

        np = NowPlaying(client=None, text_channel_id=42, state=None, db=db)
        assert np._fmt_duration(-1) == "—"
        assert np._fmt_duration(0) == "—"
        assert np._fmt_duration(45) == "0m 45s"
        assert np._fmt_duration(125) == "2m 05s"
        assert np._fmt_duration(3665) == "1h 01m"

    def test_watcher_count(self, db: Database) -> None:
        from bot.milestones import NowPlaying

        np = NowPlaying(client=None, text_channel_id=42, state=None, db=db)
        assert np._watcher_count() == 0

        # Seed some active and inactive sessions
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at) VALUES ('u1', 'user1', '2024-11-01 12:00:00')"
        )
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at, left_at) VALUES ('u2', 'user2', '2024-11-01 12:00:00', '2024-11-01 13:00:00')"
        )
        assert np._watcher_count() == 1

    async def test_post_or_replace(self, db: Database) -> None:
        from bot.milestones import NowPlaying

        channel = FakeDiscordChannel()
        client = FakeClient(channels={42: channel})
        state = FakeState()
        np = NowPlaying(client=client, text_channel_id=42, state=state, db=db)

        # First post
        track = DummyTrack()
        await np.post_or_replace(track)
        assert state.now_playing_message_id == 12345
        assert len(channel.sent_embeds) == 1
        assert channel.sent_embeds[0].title == "🎙️ Now Playing"

        # Second post should delete the previous
        msg = channel.messages[12345]
        assert not msg.deleted
        await np.post_or_replace(track)
        assert msg.deleted

    async def test_update_watcher_count(self, db: Database) -> None:
        from bot.milestones import NowPlaying

        channel = FakeDiscordChannel()
        client = FakeClient(channels={42: channel})
        state = FakeState()
        np = NowPlaying(client=client, text_channel_id=42, state=state, db=db)

        track = DummyTrack()
        await np.post_or_replace(track)

        # Update watcher count field
        db.execute(
            "INSERT INTO watch_sessions(user_id, username, joined_at) VALUES ('u1', 'user1', '2024-11-01 12:00:00')"
        )
        await np.update_watcher_count()

        msg = channel.messages[12345]
        assert len(msg.edits) == 1
        edited_embed = msg.edits[0]["embed"]
        # Find the watcher count field
        watcher_field = next(f for f in edited_embed.fields if f.name == "Currently watching")
        assert watcher_field.value == "👥 1"

    async def test_trigger_watcher_count_update_debounce(self, db: Database) -> None:
        from bot.milestones import NowPlaying

        channel = FakeDiscordChannel()
        client = FakeClient(channels={42: channel})
        state = FakeState()
        np = NowPlaying(client=client, text_channel_id=42, state=state, db=db)

        track = DummyTrack()
        await np.post_or_replace(track)

        # Trigger twice quickly
        np.trigger_watcher_count_update()
        t1 = np._update_task
        assert t1 is not None

        np.trigger_watcher_count_update()
        t2 = np._update_task
        assert t1 is t2  # Shared the same task due to debounce

        # Wait for task to finish
        await t1
        msg = channel.messages[12345]
        assert len(msg.edits) == 1


class TestAnnouncerForbidden:
    """403 Forbidden handling in the milestone announcer."""

    async def test_403_clears_channel_and_does_not_log_traceback(
        self, db: Database, caplog
    ) -> None:
        import discord

        _seed(db, "u1", alltime_seconds=5 * 3600)

        class _FakeResp:
            status = 403
            reason = "Forbidden"

        class _ForbiddenChannel:
            def __init__(self):
                self.sent = []

            async def send(self, content: str):
                self.sent.append(content)
                # Simulate a 403 Forbidden with discord error code 50001
                raise discord.Forbidden(
                    response=_FakeResp(),
                    message={"code": 50001, "message": "Missing Access"},
                )

        channel = _ForbiddenChannel()
        client = FakeClient(channels={42: channel})
        ann = MilestoneAnnouncer(
            client=client, text_channel_id=42, db=db, guild_id="1"
        )

        with caplog.at_level(logging.WARNING):
            got = await ann.check_and_announce("u1")

        assert len(got) == 1  # milestone still returned
        assert ann.text_channel_id is None  # channel cleared
        # Warning logged — no traceback
        assert any("missing access to text channel" in r.message for r in caplog.records)
        assert not any("Traceback" in r.message for r in caplog.records)


class TestNowPlayingForbidden:
    """403 Forbidden handling in Now Playing embeds."""

    async def test_post_or_replace_403_clears_channel(
        self, db: Database, caplog
    ) -> None:
        from bot.milestones import NowPlaying

        import discord

        class _FakeResp:
            status = 403
            reason = "Forbidden"

        class _ForbiddenChannel:
            def __init__(self):
                self.messages = {}
                self.sent_embeds = []

            async def fetch_message(self, msg_id: int):
                return self.messages.get(msg_id)

            async def send(self, embed):
                self.sent_embeds.append(embed)
                raise discord.Forbidden(
                    response=_FakeResp(),
                    message={"code": 50001, "message": "Missing Access"},
                )

        channel = _ForbiddenChannel()
        client = FakeClient(channels={42: channel})
        state = FakeState()
        np = NowPlaying(
            client=client, text_channel_id=42, state=state, db=db, guild_id="1"
        )

        track = DummyTrack()
        with caplog.at_level(logging.WARNING):
            await np.post_or_replace(track)

        assert np.text_channel_id is None  # channel cleared
        assert state.now_playing_message_id is None  # no message id stored
        assert any(
            "missing access to text channel" in r.message for r in caplog.records
        )

    async def test_update_watcher_count_403_clears_channel(
        self, db: Database, caplog
    ) -> None:
        from bot.milestones import NowPlaying

        import discord

        class _FakeResp:
            status = 403
            reason = "Forbidden"

        class _ForbiddenMessageForEdit:
            """A message that raises Forbidden on edit."""

            def __init__(self, id, embeds):
                self.id = id
                self.embeds = embeds

            async def edit(self, embed):
                raise discord.Forbidden(
                    response=_FakeResp(),
                    message={"code": 50001, "message": "Missing Access"},
                )

        class _ChannelWithForbiddenEdit:
            def __init__(self):
                embed = FakeEmbed(
                    title="🎙️ Now Playing",
                    description="Test",
                    fields=[
                        FakeEmbedField(
                            name="Currently watching", value="👥 0"
                        )
                    ],
                )
                self.msg = _ForbiddenMessageForEdit(id=12345, embeds=[embed])

            async def fetch_message(self, msg_id: int):
                return self.msg

            async def send(self, embed):
                return self.msg

        channel = _ChannelWithForbiddenEdit()
        client = FakeClient(channels={42: channel})
        state = FakeState()
        state.now_playing_message_id = 12345
        np = NowPlaying(
            client=client, text_channel_id=42, state=state, db=db, guild_id="1"
        )

        with caplog.at_level(logging.WARNING):
            await np.update_watcher_count()

        assert np.text_channel_id is None  # channel cleared
        assert any(
            "missing access to text channel" in r.message for r in caplog.records
        )
