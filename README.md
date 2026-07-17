# 🎙️ Discord Community TV Bot

A **24/7 autonomous audio streaming bot** for Discord. Plays a sequential
playlist into a voice channel, tracks viewer watch time, rewards engagement
with milestones, and exposes an admin dashboard.

> This is the full-blueprint implementation. See `PLAN.md` for the phase map
> and `PROGRESS.md` for a session-resumable status snapshot.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          ECOSYSTEM                                 │
│                                                                    │
│   ┌───────────────┐    HTTP    ┌───────────────┐                   │
│   │ File Provider │◀──────────▶│  TV Bot       │                   │
│   │   (FastAPI)   │            │  (discord.py) │                   │
│   │  Telethon /   │            │  + FFmpeg     │                   │
│   │  Local FS     │            └──────┬────────┘                   │
│   └──────┬────────┘                   │                            │
│          │                            │  writes                    │
│          │  cache/*.audio             ▼                            │
│          ▼                    ┌───────────────┐                    │
│   ┌───────────────┐           │  SQLite       │                    │
│   │ /cache        │           │  data/tv.db   │                    │
│   │ (10 GB LRU)   │           └───────┬───────┘                    │
│   └───────────────┘                   │ reads                      │
│                                       ▼                            │
│                              ┌───────────────┐                     │
│                              │  Dashboard    │                     │
│                              │  (FastAPI +   │                     │
│                              │   Discord     │                     │
│                              │   OAuth2)     │                     │
│                              └───────────────┘                     │
└────────────────────────────────────────────────────────────────────┘
```

Three independently-restartable services:

| Service        | What it does                                              |
| -------------- | --------------------------------------------------------- |
| `file-provider`| Owns the playlist + on-disk cache + backend fallback logic (Telethon MTProto, local FS, more coming). Serves audio file paths to the bot over HTTP. |
| `bot`          | discord.py voice bot. Joins the configured voice channel, streams audio via FFmpeg, tracks who's watching, checkpoints hourly, announces milestones. |
| `dashboard`    | Admin web UI (FastAPI + Jinja2 + Discord OAuth2). Read-only pages + skip/pause/resume controls that go through a shared SQLite command queue. |

---

## Quick start (Docker)

```bash
git clone https://github.com/YOUR-ORG/discord-radio
cd discord-radio
cp .env.example .env
# fill in DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_VOICE_CHANNEL_ID,
# DISCORD_TEXT_CHANNEL_ID, and pick a provider backend.
docker compose up -d
```

The dashboard is served on `http://localhost:8000`. Put Cloudflare / nginx /
Caddy in front for HTTPS.

For the Telegram (MTProto) backend, see [`docs/telegram-setup.md`](docs/telegram-setup.md).
For the OAuth2 admin dashboard, see [`docs/dashboard-setup.md`](docs/dashboard-setup.md).

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r file_provider/requirements.txt

# In three terminals:
make run-provider   # file provider on :8001
make run-bot        # bot connects to Discord
make run-dashboard  # dashboard on :8000
```

Run tests + lint:

```bash
make test
make lint
```

---

## Configuration

Every setting is env-var driven. See `.env.example` for the exhaustive list.
The most important ones:

| Variable                     | Purpose                                                      |
| ---------------------------- | ------------------------------------------------------------ |
| `DISCORD_BOT_TOKEN`          | Bot token from https://discord.com/developers/applications   |
| `DISCORD_GUILD_ID`           | Numeric guild id (one bot instance = one guild)              |
| `DISCORD_VOICE_CHANNEL_ID`   | Voice channel the bot joins                                  |
| `DISCORD_TEXT_CHANNEL_ID`    | Channel for Now Playing + milestone announcements            |
| `ADMIN_USER_IDS`             | Comma-separated Discord user ids allowed into the dashboard  |
| `FILE_PROVIDER_ORDER`        | Comma-separated backend order: `local`, `telegram`           |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_CHANNEL_ID` | Telegram MTProto backend |
| `LOCAL_MEDIA_PATH`           | Directory scanned by the local provider                      |
| `DASHBOARD_SECRET_KEY`       | Signing key for session cookies (`openssl rand -hex 32`)     |
| `CACHE_MAX_GB`               | Cache size ceiling (LRU eviction after)                      |

---

## Repository layout

```
bot/            discord.py bot, player, tracker, milestones, scheduler
dashboard/      FastAPI + Jinja2 dashboard, Discord OAuth2, control queue
db/             Shared SQLite layer + schema
provider/       Async HTTP client used by the bot to talk to the provider
file_provider/  Standalone FastAPI service: Telethon + Local providers + LRU cache
tests/          pytest suite (>220 tests, no live Discord/Telegram needed)
docs/           Backend setup guides
```

---

## Design decisions

* **Simplicity first.** SQLite everywhere, no separate broker/queue for the
  dashboard controls (they ride the shared DB).
* **Bot ↔ Provider decoupling.** The bot never touches Telegram directly.
  Swapping backends is a config change + a new provider class.
* **Crash safety.** Sessions get an hourly checkpoint; on startup the bot
  closes any orphan sessions using `checkpointed_at` as `left_at` so users
  never get credited for downtime.
* **Test coverage without Discord/FFmpeg.** All the interesting logic lives in
  pure modules (`bot.presence`, `bot.tracker`, `bot.milestones`, elapsed math)
  that don't import discord.py.

---

## License

MIT — see `LICENSE`.
