# PLAN.md — Discord Community TV Bot

Single source of truth: the blueprint in the project brief. Every item below maps back to a numbered section there.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Phase 0 — Foundations
- [x] Repo scaffold: `bot/`, `dashboard/`, `db/`, `provider/`, `file_provider/`, `tests/`, `data/`, `cache/` (see §13)
- [x] `.env.example` covering every var in §12 (both bot and file-provider)
- [x] `.gitignore` extended for `data/`, `cache/`, `.env`, `*.session*`, `__pycache__`, `.venv`
- [x] `requirements.txt` (bot+dashboard) + `file_provider/requirements.txt`
- [x] `pyproject.toml` with ruff + pytest config
- [x] Root `README.md` linking to component READMEs
- [x] Makefile targets: `install`, `test`, `lint`, `run-bot`, `run-dashboard`, `run-provider`

## Phase 1 — Database schema + models (§5)
- [x] `db/database.py` — connection factory, WAL mode, migrations runner
- [x] `db/models.py` — table DDLs matching §5 exactly + dataclass row types
- [x] Migration idempotency test
- [x] Basic CRUD helpers for `bot_state` (§4.2)

## Phase 2 — File Provider client contract (§4.1)
- [x] `provider/client.py` — async HTTP client (httpx) with `current()`, `next()`, `peek(n)`, `health()`, `mark_played()`
- [x] Typed response model matching §4.1 JSON contract
- [x] Retry + timeout handling; provider-down fallback surface
- [x] Unit tests using respx mock

## Phase 3 — File Provider service (§4.1 + Telegram reference)
- [x] `file_provider/api/main.py` — FastAPI with `/current`, `/next`, `/peek`, `/track/{id}`, `/health`, `/refresh`
- [x] `file_provider/providers/base.py` — abstract provider interface
- [x] `file_provider/providers/local.py` — filesystem provider (dev/tests)
- [x] `file_provider/providers/telegram.py` — Telethon MTProto adapted from hawkins-tv reference (StringSession, file cache, pre-download)
- [x] `file_provider/db.py` — SQLite for track metadata + playlist position + provider health
- [x] `file_provider/cache.py` — 10 GB LRU cache eviction
- [x] `file_provider/scheduler.py` — background pre-fetch of next track
- [x] Tests for local provider + cache eviction + API contract

## Phase 4 — Bot core (§4.2, §7)
- [x] `bot/main.py` — discord.py bot entry, intents, ready handler
- [x] `bot/player.py` — FFmpeg audio source w/ `-ss` resume, elapsed tracking, track-finished callback
- [x] `bot/state.py` — thin wrapper over `bot_state` table
- [x] `bot/config.py` — env loader
- [x] `bot/presence.py` — pure voice-transition logic (extracted for testability)
- [x] Join configured voice channel on ready; graceful shutdown
- [x] Unit tests: 33 (state 16 + player 17)

## Phase 5 — Session tracking (§6)
- [x] `bot/tracker.py` — voice_state_update handler, open/close sessions, min-threshold drop
- [x] Ignore bots; guild-scoped only
- [x] `bot/scheduler.py` — hourly checkpoint loop (§6.3)
- [x] Update `user_totals` atomically; month_key rollover safe
- [x] Recovery on startup: close orphan sessions from last run
- [x] Tests covering: short session (< threshold), long session, checkpoint, month boundary

## Phase 6 — Pause/Resume (§7.2, §7.3)
- [x] Detect last non-bot leaves → save position, stop FFmpeg (bot stays)
- [x] Detect first non-bot joins → resume from position
- [x] Handle track eviction: re-fetch via provider then resume

## Phase 7 — Milestones + Now Playing (§8, §10)
- [x] `bot/milestones.py` — check after session close and after checkpoint
- [x] Announce to text channel; idempotent via flags
- [x] Now Playing: delete previous embed, post new, save message id
- [x] Live watcher count in Now Playing embed (update on join/leave? — simplify: on track change only for V1)

## Phase 8 — Monthly reset (§9)
- [x] Scheduler task: every hour, check UTC 1st-of-month + not yet done
- [x] Snapshot into `monthly_snapshots` with rank
- [x] Reset `total_seconds_monthly`, update `month_key`
- [x] Post leaderboard summary to text channel
- [x] Tests for boundary cases (leap, timezone, already-ran-this-month)

## Phase 9 — Web dashboard (§11)
- [x] `dashboard/main.py` — FastAPI app
- [x] `dashboard/auth.py` — Discord OAuth2 (authlib), admin whitelist check
- [x] Templates: base, dashboard, leaderboard, queue (Jinja2 + Tailwind CDN)
- [x] Routes: `/`, `/login`, `/callback`, `/dashboard`, `/leaderboard`, `/queue`, `/controls/*`, `/logout`
- [x] Controls talk to bot via a small internal API (bot exposes localhost HTTP endpoint) — chose file-based command queue instead (simpler, one DB, no port coordination)
- [x] Session cookie signing with `DASHBOARD_SECRET_KEY`
- [x] CSRF protection on POST /controls/*

## Phase 10 — Docker packaging (§14)
- [x] Bot `Dockerfile` (python:3.12-slim, ffmpeg, libopus, non-root user)
- [x] File-provider `Dockerfile`
- [x] `docker-compose.yml` per §14
- [x] `.dockerignore`
- [x] Healthchecks for each service

## Phase 11 — CI + hygiene
- [x] GitHub Actions: lint + test on push (mirrors reference `.gitlab-ci.yml`)
- [x] `pytest` collects everything; coverage report artifact
- [x] `ruff check` + `ruff format --check`

## Phase 12 — Docs
- [x] Component READMEs (bot, dashboard, file_provider)
- [x] Root README with quickstart, architecture diagram, config table
- [x] `docs/telegram-setup.md` — how to get api_id/api_hash and channel id
- [x] `docs/dashboard-setup.md` — Discord OAuth2 app setup

---

## Nice-to-have / future
- [ ] Live watcher count in Now Playing embed (edit message on join/leave with rate limit)
- [ ] Additional providers (YouTube, GDrive, Torrent) per §4.1
- [ ] SSE-driven live dashboard instead of full refresh
- [ ] Discord slash commands mirroring dashboard controls
- [ ] Prometheus metrics endpoint

## Post-implementation review items
- [x] End-to-end integration test: bot's HTTP client vs real file-provider ASGI app (`tests/test_integration.py`)
- [x] End-to-end control-plane test: dashboard POST → SQLite queue → bot scheduler → player (`tests/test_control_plane.py`)
- [x] Add graceful handling in the bot when the file provider is fully down at startup (retry loop with backoff)
- [x] Guard against not-ready tracks in `Player.resume` + `_advance_and_announce` + `_resume_or_start`
- [x] Fix Player double-advance race with `_play_seq` counter (regression tests added)
- [x] Fix Provider fetch race with per-track `_fetch_lock` (regression test added)
- [x] Fix discord.py cache staleness with explicit `exclude_user_id` filter
- [x] Add per-command timeout in `Scheduler.drain_commands` (30s default)
- [x] Real smoke test of file-provider via curl (iter 17) — all endpoints verified
- [x] Real smoke test of dashboard via curl (iter 18) — all routes verified
- [ ] Verify Docker build actually succeeds locally (needs Docker in the sandbox — deferred to CI)
- [ ] `MilestoneAnnouncer.check_and_announce` — add rate-limiting so we don't spam if a user's totals rapidly cross multiple thresholds (rare; V2)
- [ ] Consider a `/health` endpoint on the dashboard (currently the compose healthcheck pings `/login`; works fine)
