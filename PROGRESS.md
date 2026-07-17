# PROGRESS.md — snapshot for a fresh session

Last updated: **iteration 15** — all 12 blueprint phases done, three review passes complete.

## Test scoreboard
- **255 tests passing** (0 failing, 0 skipped)
- **84% line coverage** — uncovered code is I/O boundary only:
  - `file_provider/providers/telegram.py` — needs live Telethon credentials
  - `bot/milestones.py NowPlaying.post_or_replace` — real discord.py embed builder
  - `bot/scheduler.py _checkpoint_loop`/`_monthly_loop`/`_command_loop` — timing loops (marked `pragma: no cover`)
- `ruff check .` clean · `ruff format --check .` clean

## Test breakdown
| Path                            | Tests | Focus                                                              |
|---------------------------------|-------|--------------------------------------------------------------------|
| tests/bot                       | 97    | player (state machine, seq race), tracker, milestones, scheduler   |
| tests/dashboard                 | 63    | OAuth flow, session signing, queries, control queue, routes        |
| tests/db                        | 16    | schema, migrations, bot_state, WAL                                 |
| tests/file_provider             | 51    | ProviderDB, LRU cache, LocalProvider, service + concurrency        |
| tests/provider                  | 19    | HTTP client contract, retry, error paths                           |
| tests/test_integration.py       | 9     | bot HTTP client ↔ real file-provider ASGI app                      |
| tests/test_control_plane.py     | 3     | dashboard POST /controls → SQLite queue → bot scheduler → player   |

## All 12 phases done
- **0** Scaffold, `.env.example`, deps, Makefile, pyproject
- **1** SQLite layer (WAL, migrations, `bot_state` kv)
- **2** `provider.client.FileProviderClient` (async httpx + retry)
- **3** `file_provider/`: FastAPI + `ProviderDB` + LRU `Cache` + `LocalProvider` + `TelegramProvider` (Telethon MTProto, from `hawkins-tv` reference) + pre-fetch thread
- **4** `bot/`: `player.py` (FFmpeg + seq-guarded after-callback), `state.py`, `config.py`, `presence.py`, `main.py`
- **5** `bot/tracker.py` — sessions, hourly checkpoint, orphan close, month rollover
- **6** Pause/resume via `should_pause`/`should_resume` + `Player.pause`/`resume`
- **7** `bot/milestones.py` — `MilestoneChecker` + `MilestoneAnnouncer` + `NowPlaying`
- **8** `bot/scheduler.py` — monthly reset with `run_monthly_reset()`
- **9** `dashboard/`: FastAPI + Discord OAuth2 + Jinja2 + control queue
- **10** Dockerfile (bot+dashboard), Dockerfile (file-provider), `docker-compose.yml`, `.dockerignore`
- **11** `.github/workflows/ci.yml` (lint + tests on 3.11/3.12 + docker build)
- **12** `README.md`, `docs/telegram-setup.md`, `docs/dashboard-setup.md`

## Bugs found & fixed during review passes
1. **Bot goes silent forever** if `provider.next()` fails → now retries with backoff up to 10 attempts.
2. **Player double-advance race**: FFmpeg's late after-callback for a superseded track could fire `on_finish` for the *new* track → added `_play_seq` counter, callback discarded if seq mismatches. Two regression tests cover this.
3. **Provider fetch race**: foreground + prefetch downloading the same file → per-track `_fetch_lock` with double-checked cache lookup. Regression test verifies exactly-once fetch under concurrency.
4. **Empty-channel pause miss**: discord.py's cached `channel.members` may still contain the departing user → `_non_bot_members(exclude_user_id=...)` explicit filter.
5. **Command queue can hang forever** on a stuck handler → per-command `asyncio.wait_for` timeout (30s default).
6. **LocalProvider symlink escape** → `resolve()` + `relative_to()` guard on both scan and fetch.
7. **Player coroutine leak** if loop closes mid-track → `loop.is_closed()` check + `coro.close()` on schedule failure.
8. **Dashboard module import opened a real DB** → replaced module-level `app = create_app()` with `_LazyApp` proxy.

## What's still deliberately out (blueprint "nice-to-have")
- Live watcher-count edits in Now Playing embed (V2 — rate-limit concerns)
- Extra providers: YouTube, GDrive, Torrent
- SSE-driven dashboard
- Discord slash-commands mirroring dashboard controls
- Prometheus metrics

## Deploy path
1. Copy `.env.example` → `.env`, fill Discord token/guild/channels, admin ids, OAuth2, provider order.
2. `docker compose up -d` — brings up file-provider, bot, dashboard.
3. Telegram backend: first-run interactive Telethon auth via `docker compose run --rm file-provider ...` — see `docs/telegram-setup.md`.
4. Put Cloudflare / nginx in front of the dashboard on :8000 (HTTPS + `--proxy-headers` already set).

## Verification-in-CI status
- `pytest -q` : 255 passing
- `ruff check .` : clean
- `ruff format --check .` : clean
- `coverage report` : 84% (see per-module breakdown in the run)
- Docker builds: not executed in this sandbox — CI builds both images.
