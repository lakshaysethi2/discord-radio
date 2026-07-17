# PROGRESS.md — snapshot for a fresh session

Last updated: **iteration 12** — all 12 blueprint phases done, hostile-review pass complete.

## Test scoreboard
- **243 tests passing** (0 failing, 0 skipped)
- **80% line coverage** (uncovered: telegram provider = live creds needed, discord.py sends = live bot needed, asyncio timing loops = marked `pragma: no cover`)
- `ruff check .` clean
- `ruff format --check .` clean

## Test breakdown by module
| Module              | Tests |
|---------------------|-------|
| tests/bot           | 78    |
| tests/dashboard     | 60    |
| tests/db            | 16    |
| tests/file_provider | 50    |
| tests/provider      | 19    |
| tests/test_integration.py | 9 (bot HTTP client ↔ real file-provider ASGI app) |

## Phases done — all 12
- **0** Scaffold, `.env.example`, `requirements.txt`, `pyproject.toml`, `Makefile`
- **1** SQLite schema + models + WAL + migrations + `bot_state` kv
- **2** `provider.client.FileProviderClient` — async httpx with retry
- **3** `file_provider/` — FastAPI, ProviderDB, LRU Cache, LocalProvider, TelegramProvider (Telethon MTProto, adapted from `hawkins-tv/tv/telegram_client.py`), pre-fetch
- **4** `bot/player.py`, `bot/state.py`, `bot/config.py`, `bot/presence.py`, `bot/main.py`
- **5** `bot/tracker.py` — sessions, checkpoints, orphan close, month rollover
- **6** Pause/resume via presence helpers + Player.pause/resume
- **7** `bot/milestones.py` — MilestoneChecker + MilestoneAnnouncer + NowPlaying
- **8** `bot/scheduler.py` — monthly reset + `run_monthly_reset()`
- **9** `dashboard/` — FastAPI + Discord OAuth2 + Jinja2 templates + control queue
- **10** `Dockerfile`, `file_provider/Dockerfile`, `docker-compose.yml`, `.dockerignore`
- **11** `.github/workflows/ci.yml` — lint + tests on 3.11/3.12 + docker build
- **12** `README.md`, `docs/telegram-setup.md`, `docs/dashboard-setup.md`

## Post-review fixes applied (iteration 11-12)
- Bot silent-forever bug: `_advance_and_announce` now retries with backoff up to 10 attempts
- `Player._after`: builds coroutine only after checking `loop.is_closed()`, prevents leak on shutdown
- `Scheduler.drain_commands`: per-command timeout (30s default) so hung command doesn't stall the queue
- `LocalProvider`: symlink-escape guard on both scan and fetch paths
- End-to-end integration test suite (`tests/test_integration.py`) — real ASGI file-provider ↔ real bot HTTP client

## What's still deliberately out
- Live watcher count refresh in Now Playing embed (V2)
- Extra providers (YouTube, GDrive, Torrent)
- SSE-driven dashboard
- Discord slash commands
- Prometheus metrics

## How a real deployment works
1. Fill `.env` from `.env.example` (Discord token/guild/channels, admin ids, OAuth2, provider order)
2. `docker compose up -d`
3. For Telegram: first-run interactive auth via `docker compose run --rm file-provider ...` (see `docs/telegram-setup.md`)
4. Put Cloudflare/nginx in front of the dashboard on :8000
5. Bot joins the voice channel, plays sequentially, tracks watch time, announces milestones, resets monthly
