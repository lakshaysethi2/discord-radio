# Handoff — gdrive/WebDAV radio migration + DinD

Status as of 2026-08-16 ~17:40 NZST. Live radio is UP (fixed by firstmate worker; Gatus green).
Repo: `/home/ubuntu/code/discord-radio`, current branch `features-and-fixes`.

## Goal
Switch the radio's primary content source from the flaky archive.org provider to gdrive
(WebDAV via `rclone serve webdav`), keep archive.org as fallback, improve playlist
continuity, run the stack in nested DinD, reduce log noise, consolidate to 2 branches,
document the outage. Keep `main` stable and listeners' continuity intact. Ship through the
no-mistakes gate.

## Blockers
1. **GitHub write access (hard blocker for push/PR/gate):**
   - `gh` token (`~/.config/gh/hosts.yml`, account `firstmate-laknz`) is expired/invalid.
   - SSH keys: only `~/.ssh/id_rsa` authenticates, as user `lakshaypbtech` — **no write access**
     to `lakshaysethi2/discord-radio` (fetch works, push rejected).
   - All local commits are on `features-and-fixes`; nothing has been pushed.
   - **Fix:** `gh auth login -h github.com` as a user with write access, then
     `git push -u origin features-and-fixes` and re-run the no-mistakes gate.
2. **Remote branch cleanup is unsafe until unique commits are folded in** (see Git state).

## Build plan (todo order)
1. **WebDAV provider** — new `file_provider/providers/webdav.py` (NOT started).
   - PROPFIND `Depth: 1` recursive listing (parse DAV XML; href, collection flag,
     getcontentlength, getcontenttype); `GET` streaming download with `.part` + atomic
     replace, mirroring `archive.py`'s `ensure_cached`.
   - `source_ref` = posix relative path under the mount; title = leaf stem;
     `has_video` via `media_types.is_video_ext`.
   - Reuse `ProviderTrack`/`ProviderFetchError` from `file_provider/providers/base.py`.
2. **Config wiring** — `file_provider/config.py` + `service.py::_providers_from_config`:
   - Env: `GDRIVE_WEBDAV_URL` (default `http://127.0.0.1:8081`), `GDRIVE_WEBDAV_PATH`
     (default `/`), optional `GDRIVE_WEBDAV_USER`/`GDRIVE_WEBDAV_PASS`.
   - `FILE_PROVIDER_ORDER=webdav,archive` (webdav primary, archive fallback). Keep archive
     fallback auto-append logic.
3. **Playlist re-point migration** — `service.py` + `db.py`:
   - `db.repoint_tracks(primary, fallback)`: for each fallback row, if a same-normalized-title
     primary row exists (normalize: lowercase, strip non-alnum), move that primary row to the
     fallback row's `sort_order` and delete the fallback row. Idempotent across refreshes.
   - Normalized title matching (plus month/year date-match for `#NN - MM_DD_YY` ↔ `apr-2002`)
     will have partial coverage given different naming; unmatched archive rows stay as fallback,
     unmatched gdrive rows append. Cursor stays valid (playlist length preserved for matches).
4. **Dashboard settings** — `dashboard/main.py` + `dashboard/templates/queue.html`:
   - Extend the existing `/controls/archive_items` pattern with a content-source form
     (provider order, gdrive webdav url/path) persisted in bot state, enqueuing
     `refresh_playlist`. Keep the re-scan button.
5. **Log reduction** — `bot/main.py::_init_logging`:
   - Default `WARNING` (was INFO); silence `httpx` and health-check access logs;
     keep Gatus push logging only on failure/transition (never per-tick success);
     keep lifecycle INFO explicitly.
6. **Tests** — mirror `tests/file_provider/test_archive_provider.py` with respx:
   - WebDAV PROPFIND parsing (nested dirs, non-playable files, video flags, size/duration),
     download URL-encoding (hashes/spaces), idempotent cache, error cleanup.
   - `repoint_tracks` unit tests (match/move/prune, idempotency, cursor preserved).
   - Config env override tests.
7. **Verify** — `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .` green.
8. **no-mistakes gate** — `no-mistakes init` (repo not initialized yet), then
   `no-mistakes axi run --intent "..."`. Expect push step to fail until blocker 1 is fixed.
9. **Parallel deploy** — start `rclone serve webdav gdrive:mother-of-all-torrents
   --addr :8081 --read-only` (host or separate container), run a second file-provider on a
   new port against a scratch DB, verify scan + /current + /next, then cut over the live
   `.env` (`FILE_PROVIDER_ORDER=webdav,archive`, `GDRIVE_WEBDAV_URL` pointing at rclone) and
   `docker compose up -d --build`. Keep gdrive-mount-watchdog for FUSE holders.
10. **DinD** — `docker-compose.dind.yml`: nested `docker:dind` running bot + file-provider +
    dashboard + rclone serve webdav; OAuth token (`~/.config/rclone/rclone.conf`) mounted
    read-only, no secrets in compose. New ports, parallel, then cutover.
11. **Postmortem** — `docs/` outage writeup (2026-08-16 gdrive FUSE death).

## Decisions locked in
- No archive.org as primary; gdrive (`gdrive:mother-of-all-torrents`) is the source.
- Archive provider code stays as fallback.
- Keep download-to-cache model; keep cache at 10 GB.
- Branches: `main` (stable) + `features-and-fixes` (dev) only; delete the rest.
- You decide remaining choices (user delegated).

## Git state
- Local: `features-and-fixes` (HEAD `f0053c4`, current), `main`, `prod` — all at `f0053c4`
  (the outage hotfix commit). **Delete local `prod`: `git branch -d prod`.**
- `f0053c4` = hotfix: 60s provider timeout, file-provider `:8001` + `gatus_default`
  network, `scripts/gdrive-mount-watchdog.sh`.
- Remote branches with commits NOT in `features-and-fixes` (fold before deleting):
  - `fm/radio-drive-fallback` (15) — includes `2ea0ad0 feat: Drive library as primary
    provider, archive.org fallback` — **review this; it may overlap with the WebDAV build.**
  - `scrub/private-estate-info` (1) — `930a4ee scrub private estate info` — fold in.
  - `fm/discord-radio-403-fix` (10), `fix/aria2-allow-overwrite` (8),
    `arena/019f6f3c-discord-radio` (6), `arena/019f785b-discord-radio` (6),
    `origin/main` (10), single commits on fm/radio-ci-e2e, gatus-heartbeat, rewind-rename,
    skip-backward, skip-forward.
  - Remote delete is blocked by blocker 1 anyway.

## Environment facts
- gdrive FUSE mount: `/home/ubuntu/mnt/google_drive` (rclone mount, healthy; `gdrive:` is
  `type = drive`, root_folder_id `0AKAW4K4SEj2FUk9PVA`).
- `gdrive:mother-of-all-torrents` = 223 playable files (106 m4b, 92 mp4, 25 mp3):
  `Lectures2002-2011/<year>/<mon>-<year>.mp4`, `VolumeSeries/*.mp4`, `Satsangs/`,
  `Ontheroad/`, `Archivalofficeseries/`, `DocandSusantalks/`, `Audiobooks/`.
- archive.org item `Hawkins_Lectures_transcoded_actual_files` = 640 originals
  (335 mp3, 256 mp4, 2 m4b, 3 webm; dirs `BTO Radio Interviews/`, `David R. Hawkins/`,
  `Hawkins_Lectures_transcoded/`, `Satsang Series 2006/`, `audiobooks/`...). Naming differs
  from gdrive — migration match will be partial.
- `rclone` v1.50.2 at `/usr/bin/rclone`; config `~/.config/rclone/rclone.conf` (contains
  OAuth token — mask when showing). Host rclone serve http `:8080` is actually copyparty
  (302 to ./login), not rclone.
- Existing host services: file-provider on `:8001`, dashboard, bot (docker compose).
- `.env`: `FILE_PROVIDER_ORDER=local` (needs → `webdav,archive`), `ARCHIVE_ORG_ITEMS=...`,
  `DATABASE_PATH=/data/tv.db`, `FILE_PROVIDER_DB_PATH=/data/provider.db`,
  `GATUS_PUSH_INTERVAL_SECONDS=5`.
- Tests: `.venv/bin/python -m pytest -q`; lint `.venv/bin/ruff check .`. `pyproject.toml`
  ruff line-length 100, selects E/F/W/I/B/UP/SIM/RUF.
- Content data cached at `/tmp/opencode/moat_ls.json` (gdrive file list) and
  `/tmp/opencode/archive_meta.json` (archive.org item metadata) if still present.

## Additional context (distilled from investigation)
- **SLA hard rule (AGENTS.md):** never silent >10s with a listener in voice (5s ideal);
  pause when 0 listeners; `bot/gatus_heartbeat.py` `SILENCE_THRESHOLD_SECONDS = 10.0`,
  Gatus tick every 5s. Do not regress.
- **Live state numbers:** provider DB holds 908 archive tracks, cursor ~766–908; cache near
  10 GB max. When switching providers, the cursor is a 0-indexed position into the sorted
  playlist — re-pointing matched rows at the same `sort_order` keeps it in range.
- **Bot refresh wiring:** `bot/main.py` `_handle_command` → `refresh_playlist` (~line 740)
  reads `BotStateKey.ARCHIVE_ORG_ITEMS` from `db` and calls `provider.refresh(...)`
  (provider/client.py). Dashboard `/controls/archive_items` sets that state + enqueues
  `refresh_playlist`. Extend this same pattern for gdrive settings.
- **Provider contract:** `file_provider/providers/base.py` — `ProviderTrack(title,
  source_ref, duration_seconds, size_bytes, has_video)`, `track_id = "<name>_<sha1-16>"`,
  `BaseProvider` (list_tracks / ensure_cached / is_configured), `ProviderFetchError`.
  Tests use respx + `tests/file_provider/conftest.py` `FakeProvider` fixture.
- **no-mistakes gate:** repo not initialized — run `no-mistakes init` first. Then
  `no-mistakes axi run --intent "<user goal + decisions>"`. It blocks at gates; respond
  with `axi respond --action approve|fix|skip`. `--yes` = unattended. Escalate
  `ask-user` findings. Expect the push step to fail until GitHub auth is fixed.
- **Security note:** the rclone OAuth access/refresh token in `~/.config/rclone/rclone.conf`
  was printed unmasked in an earlier terminal session — consider rotating it.
- **Uncommitted work:** `HANDOFF.md` itself is untracked. Working tree otherwise clean.

## Recommended immediate steps
1. Review `git show origin/fm/radio-drive-fallback` (esp. `2ea0ad0`) — decide fold vs build.
2. Write `file_provider/providers/webdav.py` + config wiring (items 1–2 above).
3. Fix GitHub auth, then push/PR via no-mistakes.
