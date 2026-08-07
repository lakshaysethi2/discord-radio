# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Architecture pointers

- Slash commands live in `bot/commands.py`: `build_commands(db, provider, state, radio, stations)` returns `(name, desc, callback)` tuples registered + globally synced in `bot/main.py`'s `on_ready`. Command callbacks that need run()-scoped state (the shared `_advance_lock`, the live `admin_paused` flag) receive it injected as a callable param — see `/forward`'s `forward_radio` and `/rewind`'s `rewind_radio` for the pattern (the two are exact mirrors).
- One shared playback cursor: `RadioClock` in `bot/main.py` owns the global position (`seek`/`start`/`pause`/`reset`); every guild's player just joins at `radio.position()`. Any position-changing mutation must run under `_advance_lock` to avoid racing the natural end-of-track advance (`_advance_and_announce`).
- Scheduler (dashboard) commands are handled by `_handle_command` in `run()`; slash commands are separate.

## Testing

- `.venv/bin/python -m pytest -q` (full suite) and `.venv/bin/ruff check .` must stay green.
- `tests/bot/` uses fake stations/players/voice clients + monkeypatched `time.monotonic` for `RadioClock` maths (see `tests/bot/test_forward.py`, `tests/bot/test_rewind.py`, `tests/bot/test_radio_clock.py`).

## Hawkins Radio SLA — Devotional Non Duality (DND) — HARD RULE (2026-08-08)

- **Pause when empty, never silent when occupied:** `listener_count == 0` → radio `pause()` (expected silence, Gatus green). `listener_count >= 1` in the dedicated DND voice channel → radio MUST be playing audio; silence > **10s is a failure** (5s ideal, 10s hard limit — captain: listeners came to hear the radio, not silence). 60s was way too much and caused false-green monitoring.
- **Monitoring enforces 10s:** `bot/gatus_heartbeat.py` `SILENCE_THRESHOLD_SECONDS = 10.0`; `is_radio_healthy(stations, now, threshold)` tracks `silence_since` per station (monotonic) and returns `False` after 10s silent with listener; `GatusHeartbeat` ticks every **5s** (default `GatusPushIntervalSeconds = 5`, env `GATUS_PUSH_INTERVAL_SECONDS=5` in live `.env`) and pushes `success=false` immediately on violation so Gatus `radio_discord-radio-voice` (`https://gatus.lak.nz/endpoints/radio_discord-radio-voice`) turns **red within 10s**, not next cycle. Threshold/tick are clamped: tick >10s forced to 5s.
- **Live wiring:** `bot/main.py` `health_check=lambda: is_radio_healthy(stations)` via `GatusHeartbeat.run()`; `bot/config.py` default `gatus_push_interval_seconds=5`; `sync_radio_state()` resumes from frozen `radio.position()` when listeners appear. Verify with: `docker compose up -d --build bot` → `docker logs tvbot-bot` (tick silence_seconds) + `curl https://gatus.lak.nz/api/v1/endpoints/statuses` (radio group success false when silent with listener).
- **Do not regress:** never raise threshold above 10s, never slow tick above 5s, never return healthy when `listener_count>0` and silent — that is the false-positive that hid outages. Commit `9e3ecdb` on `prod`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
