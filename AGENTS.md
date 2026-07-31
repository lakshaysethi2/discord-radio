# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Architecture pointers

- Media source: the estate Drive library is mounted read-only at `/media` by the
  `rclone-mount` compose service (rclone FUSE of `gdrive:mother-of-all-torrents`,
  rshared propagation); `file-provider` binds it ro and `FILE_PROVIDER_ORDER=local`
  makes it primary, with archive.org auto-appended as fallback. The local scan
  (`LocalProvider.list_tracks`) tolerates unreadable dirs; `/media` must never be
  recursively chowned (hangs on the FUSE mount). Playlist refresh is manual
  (`POST /refresh` or dashboard) — a provider restart does NOT rescan when the
  playlist is non-empty.

- Slash commands live in `bot/commands.py`: `build_commands(db, provider, state, radio, stations)` returns `(name, desc, callback)` tuples registered + globally synced in `bot/main.py`'s `on_ready`. Command callbacks that need run()-scoped state (the shared `_advance_lock`, the live `admin_paused` flag) receive it injected as a callable param — see `/forward`'s `forward_radio` and `/rewind`'s `rewind_radio` for the pattern (the two are exact mirrors).
- One shared playback cursor: `RadioClock` in `bot/main.py` owns the global position (`seek`/`start`/`pause`/`reset`); every guild's player just joins at `radio.position()`. Any position-changing mutation must run under `_advance_lock` to avoid racing the natural end-of-track advance (`_advance_and_announce`).
- Scheduler (dashboard) commands are handled by `_handle_command` in `run()`; slash commands are separate.

## Testing

- `.venv/bin/python -m pytest -q` (full suite) and `.venv/bin/ruff check .` must stay green.
- `tests/bot/` uses fake stations/players/voice clients + monkeypatched `time.monotonic` for `RadioClock` maths (see `tests/bot/test_forward.py`, `tests/bot/test_rewind.py`, `tests/bot/test_radio_clock.py`).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
