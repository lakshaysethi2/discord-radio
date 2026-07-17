# PROGRESS.md — snapshot for a fresh session

Last updated: **iteration 5** — Phase 4 (bot core) starting.

## Test scoreboard
- Total: **83 passing** (db 16 + provider 17 + file_provider 50)
- Lint: `ruff check .` clean, `ruff format --check .` clean

## Phases done
- **Phase 0** — repo scaffold, deps, Makefile, pyproject, `.env.example`
- **Phase 1** — `db/` SQLite layer, all §5 tables, WAL, migrations, bot_state kv
- **Phase 2** — `provider/client.py` async httpx client for file provider (retry + typed responses)
- **Phase 3** — `file_provider/` FastAPI service, `ProviderDB`, `Cache` (LRU), `LocalProvider`, `TelegramProvider` (Telethon MTProto from hawkins-tv reference), pre-fetch thread

## What's next
1. Phase 4 — bot/main.py + bot/player.py + bot/state.py (discord.py + FFmpeg)
2. Phase 5 — bot/tracker.py + bot/scheduler.py (voice_state_update, checkpoints)
3. Phase 6 — pause/resume (thin — most logic falls out of Phase 4)
4. Phase 7 — milestones + Now Playing message
5. Phase 8 — monthly reset
6. Phase 9 — dashboard/ FastAPI + Discord OAuth2 + Jinja2 templates
7. Phase 10 — Dockerfiles + docker-compose
8. Phase 11 — CI (GH Actions)
9. Phase 12 — docs

## Key design decisions locked in
- Bot ↔ file-provider: HTTP over `provider.client.FileProviderClient` (never touches Telegram directly)
- Dashboard ↔ bot control-plane: **shared SQLite `dashboard_commands` queue** (no internal HTTP port between bot and dashboard)
- Reference `hawkins-tv/tv/telegram_client.py` — StringSession, per-thread event loop, `iter_download`
- `db/models.py` has `SCHEMA` tuple + `BOT_STATE_KEYS` frozenset for lookup safety

## Repo shape
```
bot/            (empty — Phase 4)
dashboard/      (empty — Phase 9)
db/             (Phase 1 — done)
provider/       (Phase 2 — done, client for bot to talk to file-provider)
file_provider/  (Phase 3 — done, standalone FastAPI service + Telethon)
tests/          (83 tests)
```

## Known nits/future items in PLAN.md nice-to-have
- Live watcher count in Now Playing embed (skipped for V1)
- Additional providers (YouTube, GDrive, Torrent)
- SSE-driven dashboard
