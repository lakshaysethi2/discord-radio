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

## Quick start

**Requirements:** Docker + `make`. Nothing else runs on the host.

```bash
git clone https://github.com/YOUR-ORG/discord-radio
cd discord-radio
make env             # creates .env from .env.example
$EDITOR .env         # fill in Discord token, guild id, channel ids, admin ids
make up              # brings up file-provider → bot → dashboard
make logs            # tail everything
make health          # container status
```

The dashboard is served on `http://localhost:8000`. Put Cloudflare / nginx /
Caddy in front for HTTPS.

### Everyday ops

Every command runs inside a container — the host only needs Docker + make.

| Command                     | What it does                                        |
| --------------------------- | --------------------------------------------------- |
| `make up`                   | Start file-provider, bot, dashboard                 |
| `make up-build`             | Rebuild images then start                           |
| `make down`                 | Stop everything (keeps volumes)                     |
| `make restart`              | Restart all containers                              |
| `make rebuild`              | Full no-cache rebuild                               |
| `make logs` / `logs-bot`    | Tail logs (all / just bot)                          |
| `make ps` / `health`        | Container status                                    |
| `make test`                 | Run pytest inside a container                       |
| `make test-cov`             | Run pytest with coverage report                     |
| `make lint`                 | Ruff check + format-check                           |
| `make format`               | Ruff format + autofix                               |
| `make dev`                  | Interactive bash inside a dev container             |
| `make shell-bot`            | Shell into the running bot container                |
| `make db-shell`             | `sqlite3 /data/tv.db` inside the bot container      |
| `make refresh-playlist`     | Tell the file-provider to rescan                    |
| `make telegram-login`       | First-run interactive Telethon auth                 |
| `make backup`               | tar.gz of `data/` + `cache/` in `backups/`          |

Run `make help` to see everything.

Backend setup guides:

- **archive.org (public HTTP, no auth)** — [`docs/archive-org-setup.md`](docs/archive-org-setup.md)
- **Telegram (MTProto via Telethon)** — [`docs/telegram-setup.md`](docs/telegram-setup.md)
- **Admin OAuth2 dashboard** — [`docs/dashboard-setup.md`](docs/dashboard-setup.md)

---

## Local development

Same commands as production — everything runs in containers.

```bash
make dev             # drop into an interactive bash inside a dev container
                     # ... /app is the mounted source tree
                     # ... run python, pytest, ruff, sqlite3 as needed

# Or just:
make test            # pytest inside a container
make lint            # ruff check + format-check
make format          # ruff format + autofix
```

Source edits on the host are visible instantly inside the container (bind
mount). No host virtualenv or Python install needed.

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
| `FILE_PROVIDER_ORDER`        | Comma-separated backend order: `local`, `archive`, `telegram` |
| `ARCHIVE_ORG_ITEMS`          | Comma-separated Internet Archive item ids (public, no auth)  |
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

## CI

A ready-to-use GitHub Actions workflow lives at [`ci/github-actions.yml`](ci/github-actions.yml).
It runs ruff + pytest on Python 3.11 and 3.12 and does a Docker build sanity
check. To activate:

```bash
mkdir -p .github/workflows
mv ci/github-actions.yml .github/workflows/ci.yml
git commit -am "Activate CI" && git push
```

(It lives outside `.github/` so a restricted GitHub App can push the rest of
the repo without needing the `workflows` scope.)

## License

MIT — see `LICENSE`.
