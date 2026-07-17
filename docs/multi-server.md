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
| `/servers` (dashboard) | Lists every discovered server with an enable toggle + voice/text `<select>`s; saves to `guild_configs` and live-applies via the `apply_server` control-plane command. |

Because the radio is shared, controls (`skip` / `pause` / `resume` / `volume` /
`play`) act on the stream and are fanned out to every server that currently has
listeners. Pause/resume is decided **per server** from that server's own voice
channel occupancy.

---

## First run (optional env bootstrap)

These are **optional** — leave them unset to configure everything from the
dashboard. If you do set the legacy env vars, they are used as a one-time seed:

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
     If you leave this blank, it defaults to the voice channel's own text chat
     (Discord nests a text chat under the voice channel when "text chat in
     voice" is enabled).
   - Click **Save**.
3. The bot picks the change up within a few seconds — it joins/leaves the
   voice channel and repoints *Now Playing* on the fly. **No restart needed.**
   (If the bot is offline when you save, the change is applied automatically the
   next time it starts.)

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
  is actually in the server and online; channels are refreshed on `on_ready`.
* **Saved but bot still silent** — server changes apply live (the bot polls
  the `apply_server` command within a couple of seconds), so first confirm the
  bot process is running. Then check the log for `voice connect attempt` /
  `giving up on voice connection` — that means the bot role lacks **Connect +
  Speak** in the chosen voice channel, or this host can't reach Discord's voice
  servers over UDP.
* **"no enabled servers with valid channels" in the bot log** — enable a server
  and pick valid voice + text channels in the dashboard.
