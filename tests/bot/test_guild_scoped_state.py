"""GuildScopedState isolates each server's Now Playing embed message id."""

from __future__ import annotations

from bot.state import BotState, GuildScopedState


def test_guild_scoped_message_id_isolated(db) -> None:
    gs = GuildScopedState(db, "g1")
    gs.now_playing_message_id = 111
    assert gs.now_playing_message_id == 111
    # The shared/global key must be untouched.
    base = BotState(db)
    assert base.now_playing_message_id is None


def test_different_guilds_isolated(db) -> None:
    a = GuildScopedState(db, "a")
    b = GuildScopedState(db, "b")
    a.now_playing_message_id = 1
    b.now_playing_message_id = 2
    assert a.now_playing_message_id == 1
    assert b.now_playing_message_id == 2


def test_empty_guild_id_uses_global_key(db) -> None:
    gs = GuildScopedState(db, "")
    gs.now_playing_message_id = 42
    # With no guild, it should behave exactly like the base BotState.
    assert BotState(db).now_playing_message_id == 42
