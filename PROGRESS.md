# PROGRESS.md — snapshot for a fresh session

Last updated: **iteration 21** — fixed SQLite connection concurrency / cursor corruption bugs, added live debounced watcher count edits to the Now Playing embed, and expanded tests.

## Test scoreboard
- **285 tests passing** (0 failing, 0 skipped)
- `ruff check .` clean · `ruff format --check .` clean
- `make test`, `make lint`, `make help` all work

## Test breakdown
| Path                            | Tests | Focus                                                              |
|---------------------------------|-------|--------------------------------------------------------------------|
| tests/bot                       | 110   | player (state machine, seq race, not-ready guards), tracker, milestones (checker + announcer + now playing), scheduler + drain, config, presence, main helpers (startup retry) |
| tests/dashboard                 | 63    | OAuth flow, session signing, queries, control queue, all routes    |
| tests/db                        | 16    | schema, migrations, bot_state kv, WAL                              |
| tests/file_provider             | 51    | ProviderDB, LRU cache, LocalProvider, service + concurrency        |
| tests/provider                  | 19    | HTTP client contract, retry, error paths                           |
| tests/test_integration.py       | 9     | bot HTTP client ↔ real file-provider ASGI app                      |
| tests/test_control_plane.py     | 3     | dashboard POST /controls → SQLite queue → bot scheduler → fake player |

## Smoke tests (real running processes)
- **File provider on 127.0.0.1:18001** (iter 17): scanned 3 local mp3s, `/current`, `/next`, `/peek`, `/health` all returned correct JSON; cache hardlinks confirmed via `ls -la` (link count 2)
- **Dashboard on 127.0.0.1:18000** (iter 18): `/` → 307 to `/dashboard`; unauth `/dashboard` → 307 to `/login`; `/login` → 307 to `discord.com/oauth2/authorize` with all params + signed state cookie; `/callback` without code → renders login with error; POST `/controls` unauth → 307 to `/login`

## All 12 phases done
- **0** Scaffold, `.env.example`, deps, Makefile, pyproject
- **1** SQLite layer (WAL, migrations, `bot_state` kv)
- **2** `provider.client.FileProviderClient` (async httpx + retry)
- **3** `file_provider/`: FastAPI + `ProviderDB` + LRU `Cache` + `LocalProvider` + `TelegramProvider` (Telethon MTProto, adapted from `hawkins-tv`) + pre-fetch thread with per-track locks
- **4** `bot/`: `player.py` (FFmpeg + seq-guarded after-callback), `state.py`, `config.py`, `presence.py`, `main.py`
- **5** `bot/tracker.py` — sessions, hourly checkpoint, orphan close, month rollover
- **6** Pause/resume via `should_pause`/`should_resume` + `Player.pause`/`resume`
- **7** `bot/milestones.py` — `MilestoneChecker` (pure) + `MilestoneAnnouncer` (Discord I/O) + `NowPlaying` embed
- **8** `bot/scheduler.py` — `run_monthly_reset()` with multi-month snapshotting
- **9** `dashboard/`: FastAPI + Discord OAuth2 + Jinja2 + shared-SQLite control queue
- **10** Dockerfile (bot+dashboard), Dockerfile (file-provider), `docker-compose.yml`, `.dockerignore`
- **11** `ci/github-actions.yml` (lint + tests on py3.11/3.12 + docker build; move to `.github/workflows/` to activate)
- **12** `README.md`, `docs/telegram-setup.md`, `docs/dashboard-setup.md`

## Bugs found & fixed across four review passes
1. Bot silent-forever if `provider.next()` fails → retry with backoff (10 attempts).
2. Player double-advance race: superseded track's late after-callback fired on_finish for the *new* track → `_play_seq` guard, two regression tests.
3. Provider fetch race: foreground + prefetch downloading same file → per-track `_fetch_lock` + double-checked cache lookup, regression test verifies exactly-once fetch.
4. Empty-channel pause miss: discord.py's stale member cache → `_non_bot_members(exclude_user_id=...)` explicit filter.
5. Command queue hang: stuck handler blocked whole poll loop → per-command `asyncio.wait_for(timeout=30s)`.
6. LocalProvider symlink escape → `resolve()` + `relative_to()` guard on both scan and fetch.
7. Player coroutine leak on shutdown mid-track → `loop.is_closed()` check + `coro.close()`.
8. Dashboard module-level `app = create_app()` opened a real DB on import → `_LazyApp` proxy.
9. Not-ready tracks starting playback → guards in `Player.resume`, `_advance_and_announce`, `_resume_or_start`.
10. Bot startup with file-provider down → exponential-backoff retry (20 attempts, up to 30s).
11. FK on `cache_entries` prevented `rebuild_from_disk` and standalone cache tests → dropped FK, documented.
12. `on_ready` fires again on reconnect → guard flag prevents re-init.

## What's still deliberately out (blueprint "nice-to-have")
- Live watcher-count edits in Now Playing embed (V2 — rate-limit concerns)
- Extra providers: YouTube, GDrive, Torrent
- SSE-driven dashboard
- Discord slash-commands mirroring dashboard controls
- Prometheus metrics
- Rate-limit on milestone announcements (rare edge case)

## Deploy path (host needs only Docker + make)
1. `make env` — creates `.env` from `.env.example`.
2. Edit `.env` — Discord token/guild/channels, admin ids, OAuth2, provider order.
3. `make up` — brings up file-provider → bot → dashboard (with proper healthcheck ordering).
4. For Telegram backend: `make telegram-login` for first-run interactive Telethon auth (see `docs/telegram-setup.md`).
5. Put Cloudflare / nginx in front of dashboard on :8000 (HTTPS terminator; `--proxy-headers` already set).

## How to resume in a fresh session
1. `git status` — should be clean on `arena/019f6df3-discord-radio`.
2. `make build` — builds all Docker images.
3. `make test` — runs the 263 tests inside a container.
4. `make lint` — ruff check + format check inside a container.
5. Check `PLAN.md` for outstanding items (all core work done; only nice-to-haves remain).
