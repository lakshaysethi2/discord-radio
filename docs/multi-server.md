# Multi-server (per-guild) management

The bot can serve several Discord servers from a single process. One shared
playlist cursor drives every server — they all hear the same "radio" — but each
server gets its own voice connection, *Now Playing* embed, and milestone
announcements.

This guide covers how admins decide **which servers** the bot speaks in and
**which voice/text channels** it uses, entirely from the dashboard.

---

## How it works

| Layer | What it does |
| ----- | ------------ |
| `guild_configs` (DB) | One row per server the bot belongs to: `enabled`, `voice_channel_id`, `text_channel_id`. |
| `guild_channels` (DB) | Cached list of each server's channels, refreshed from Discord on every `on_ready` so the dashboard can render dropdowns without calling Discord. |
| `on_ready` (bot) | Discovers guilds + channels, seeds the env vars once, then joins every *enabled* server that has both channels selected. |
| `Station` (bot) | Per-server object bundling a `Player`, `NowPlaying` embed, and `MilestoneAnnouncer`. Sessions are tracked per `guild_id`. |
| `/servers` (dashboard) | Lists every discovered server with an enable toggle + voice/text `<select>`s; saves to `guild_configs`. |

Because the radio is shared, controls (`skip` / `pause` / `resume` / `volume` /
`play`) act on the stream and are fanned out to every server that currently has
listeners. Pause/resume is decided **per server** from that server's own voice
channel occupancy.

---

## First run (env bootstrap)

If you set the legacy env vars, they are used as a one-time seed:

```
DISCORD_GUILD_ID=123
DISCORD_VOICE_CHANNEL_ID=456
DISCORD_TEXT_CHANNEL_ID=789
```

On first boot the bot will find that server, confirm those channels exist, and
write an **enabled** `guild_configs` row so the bot starts speaking there
immediately. From then on the dashboard owns that row — editing it in the
dashboard will not be overwritten by the env vars.

Leave the env vars blank to start with **zero servers enabled** and configure
everything from the dashboard.

---

## Managing servers from the dashboard

1. Open the dashboard and click **Servers** in the nav.
2. Every server the bot is a member of is listed. For each:
   - **Allow bot to speak here** — toggle on/off.
   - **Voice channel (join)** — pick where the bot connects.
   - **Text channel (updates)** — pick where *Now Playing* + milestones post.
   - Click **Save**.
3. Restart the bot so it re-reads `guild_configs` and (re)connects.

The dashboard only ever stores channel ids it actually discovered for that
server, so a forged form can't point the bot at an arbitrary channel.

---

## Data model notes

* `watch_sessions` gained a `guild_id` column (default `''`). Watcher counts and
  milestone announcements are scoped per server; community-wide watch time in
  `user_totals` remains global.
* Each server's *Now Playing* embed message id is stored under a per-guild key
  (`now_playing_message_id:<guild_id>`) so servers don't clobber each other's
  embeds.
* The global `is_paused` flag means "nobody is listening anywhere".

---

## Troubleshooting

* **Server not listed** — the bot hasn't discovered it yet. Make sure the bot
  is actually in the server and online, then restart.
* **Saved but bot still silent** — server management changes apply on bot
  restart (`on_ready`), not live.
* **"no enabled servers with valid channels" in the bot log** — enable a server
  and pick valid voice + text channels in the dashboard, then restart.
