"""Discord TV Bot package.

Structure:
    bot.main       — discord.py entry point, event wiring
    bot.player     — FFmpeg wrapper, elapsed tracking, resume math
    bot.state      — thin key/value adapter over the `bot_state` table
    bot.tracker    — voice_state_update handler (opens/closes sessions)
    bot.scheduler  — background tasks (hourly checkpoint, monthly reset)
    bot.milestones — milestone detection + Discord announcements
    bot.config     — environment loader

Everything except `main.py` is written so it can be unit-tested without a live
Discord connection.
"""
