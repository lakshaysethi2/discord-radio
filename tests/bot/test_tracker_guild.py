"""Per-guild scoping of watch sessions (§servers)."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.tracker import SessionTracker

UTC = UTC


def dt(y=2024, m=6, d=1, h=12, mi=0, s=0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def test_guild_id_defaults_empty(db) -> None:
    t = SessionTracker(db, min_session_seconds=30)
    t.open_session(user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt())
    row = db.fetchone("SELECT guild_id FROM watch_sessions WHERE user_id='u1'")
    assert row["guild_id"] == ""


def test_per_guild_sessions_are_independent(db) -> None:
    t = SessionTracker(db, min_session_seconds=30)
    t.open_session(
        user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=10), guild_id="g1"
    )
    t.open_session(
        user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=11), guild_id="g2"
    )
    rows = db.fetchall("SELECT * FROM watch_sessions WHERE user_id='u1' AND left_at IS NULL")
    assert len(rows) == 2

    # Closing g1 must not touch g2's session.
    closed = t.close_session(user_id="u1", now=dt(h=12), guild_id="g1")
    assert closed is not None
    rows = db.fetchall("SELECT * FROM watch_sessions WHERE user_id='u1' AND left_at IS NULL")
    assert len(rows) == 1
    assert rows[0]["guild_id"] == "g2"


def test_close_session_default_does_not_cross_guild(db) -> None:
    t = SessionTracker(db, min_session_seconds=30)
    t.open_session(
        user_id="u1", username="A", server_nickname=None, track_id="t1", now=dt(h=10), guild_id="g2"
    )
    # Closing without a guild_id (default "") must not find g2's session.
    assert t.close_session(user_id="u1", now=dt(h=12)) is None


def test_watcher_count_per_guild(db) -> None:
    from bot.milestones import NowPlaying

    t = SessionTracker(db, min_session_seconds=30)
    t.open_session(user_id="u1", username="A", server_nickname=None, track_id="t1", guild_id="g1")
    t.open_session(user_id="u2", username="B", server_nickname=None, track_id="t1", guild_id="g2")
    np_g1 = NowPlaying(client=None, text_channel_id=1, state=None, db=db, guild_id="g1")
    np_g2 = NowPlaying(client=None, text_channel_id=1, state=None, db=db, guild_id="g2")
    np_all = NowPlaying(client=None, text_channel_id=1, state=None, db=db)
    assert np_g1._watcher_count() == 1
    assert np_g2._watcher_count() == 1
    assert np_all._watcher_count() == 2
