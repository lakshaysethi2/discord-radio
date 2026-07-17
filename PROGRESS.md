# PROGRESS.md — snapshot for a fresh session

Last updated: **iteration 22** — added multi-server (per-guild) management:
the bot now serves many Discord servers from one process (shared radio, per-server
voice/text/embeds/milestones), admins manage it from the dashboard **Servers**
page, and the legacy single-guild env vars became an optional one-time bootstrap.

## Test scoreboard
- **355 tests passing** (0 failing, 0 skipped)
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
1. `git status` — should be clean on `arena/019f6f2f-discord-radio`.
2. `make build` — builds all Docker images.
3. `make test` — runs the 263 tests inside a container.
4. `make lint` — ruff check + format check inside a container.
5. Check `PLAN.md` for outstanding items (all core work done; only nice-to-haves remain).

## Queue playlist delivery — 2026-07-17
- Recovered and implemented the previously unpushed queue work on the current Arena branch.
- `/queue` now pages through the complete playlist (50 per page by default), supports title search, highlights the persisted active track, offers a jump-to-active shortcut, shows media/cache status, and queues CSRF-protected `play_track` actions.
- File-provider adds metadata-only `GET /tracks` and fetch-on-select `POST /jump/{track_id}`. The bot consumes `play_track` through the shared SQLite command queue.
- Review fix: dashboard obtains active-track state from its shared bot SQLite DB rather than provider `/current`, so merely browsing the playlist never downloads uncached media.
- Verification in this sandbox (Docker unavailable): `.venv/bin/python -m pytest -q` → **313 passed**; `ruff check .` clean; `ruff format --check .` clean.

## Multi-server delivery — 2026-07-17
- The bot is no longer hard-coded to one guild. On `on_ready` it discovers
  every server it belongs to, caches their channels into `guild_channels`, and
  joins each *enabled* server that has both a voice + text channel selected in
  `guild_configs`.
- One shared playback cursor drives all servers (the "radio"); each server gets
  its own `Station` (voice connection, `NowPlaying` embed, `MilestoneAnnouncer`).
  Sessions are tracked per `guild_id`; `GuildScopedState` keeps each server's
  Now Playing embed isolated.
- Dashboard **Servers** page (`/servers`) lets admins toggle a server on/off and
  pick its voice + text channels; the save is CSRF-protected and only ever
  stores channel ids the bot actually discovered for that server.
- Legacy `DISCORD_GUILD_ID` / `DISCORD_VOICE_CHANNEL_ID` / `DISCORD_TEXT_CHANNEL_ID`
  are now an optional one-time bootstrap (seeded as enabled on first boot), then
  the dashboard owns the config.
- New tests: `tests/db/test_guilds.py`, `tests/db/test_guild_tables.py`,
  `tests/bot/test_tracker_guild.py`, `tests/bot/test_guild_scoped_state.py`,
  `tests/bot/test_scheduler_guild.py`, `tests/dashboard/test_servers.py`.
- Verification: `.venv/bin/python -m pytest -q` → **347 passed**; `ruff check .`
  clean; `ruff format --check .` clean.

## Review fixes (PR #4) — 2026-07-17
Addressed the three items raised in code review:
- **Shared-cursor correctness (High):** added `RadioClock`, the single
  authoritative playback clock, decoupled from any `Player`'s per-voice-client
  clock. Stations now join/resume at `radio.position()` so every server hears
  the same track at the same offset; the global position is only persisted by
  the orchestrator on play/pause transitions. `Player` no longer writes the
  global position/is_paused when `persist_pause_state=False` (so an idle
  server can't corrupt the cursor). New tests in `tests/bot/test_radio_clock.py`
  cover the "Guild A has played N seconds → Guild B joins at ~N seconds" case.
- **Checkpoint crash (High):** `sqlite3.Row` has no `.get()`; the scheduler's
  checkpoint loop now indexes `row["guild_id"]` and the loop body was
  extracted to `Scheduler._announce_open_sessions()` (covered by a real-`Row`
  test in `tests/bot/test_scheduler_guild.py`).
- **Channel-type validation (Medium):** `/servers/update` now validates the
  voice id against *voice* channels and the text id against *text* channels
  (not a combined set), and refuses to persist an `enabled` config that lacks
  one valid voice + one valid text channel (HTTP 400). Tests updated in
  `tests/dashboard/test_servers.py`.

Verification after fixes: `.venv/bin/python -m pytest -q` → **354 passed**;
`ruff check .` clean; `ruff format --check .` clean.

## Manual-pause regression fix (2nd review pass) — 2026-07-17
The second review pass found one remaining regression: the dashboard **pause**
command stopped every station player but never froze the authoritative
`RadioClock`, so the shared clock kept advancing while "paused". A later
**resume** then re-joined all stations at the wall-clock position — silently
skipping the paused interval.
- Introduced an in-memory `admin_paused` flag (independent of listener count)
  and a single `sync_radio_state()` routine that is now the one place the
  shared radio is frozen/resumed: the radio plays only when
  `has_listeners and not admin_paused`. `pause` sets `admin_paused=True` and
  freezes the clock; `resume` clears it and restarts the clock from the frozen
  offset before re-joining each live station at `radio.position()`. Voice-state
  changes call the same routine (without clearing the manual-pause flag), and
  the `JOINED` resume is gated on `not admin_paused` so an admin pause keeps
  every server silent until explicitly resumed. `play_track` also clears the
  manual pause (playing a chosen track is an explicit play intent).
- Regression test `tests/bot/test_radio_clock.py::
  test_dashboard_pause_freezes_clock_and_resume_keeps_position` reproduces the
  exact scenario (radio at 30s → admin pause → 5 min wall-clock passes → admin
  resume) and asserts the resumed seek is 30s, not 330s.

Verification: `.venv/bin/python -m pytest -q` → **355 passed**;
`ruff check .` clean; `ruff format --check .` clean.
