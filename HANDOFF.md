# Ponytail audit — over-engineering

Whole-repo scan. Findings only; nothing applied. Ranked biggest cut first.

`delete:` Telegram backend — live order is `webdav,archive`; never loaded. Nothing. [file_provider/providers/telegram.py] (+ telethon, cryptg, docs/telegram-setup.md, Makefile `telegram-login`)

`delete:` Bot `tracks` table + unused row types (`Track`/`WatchSession`/`UserTotals`/`MonthlySnapshot`/`DashboardCommand`). Provider DB owns playlist; queries use `sqlite3.Row`. [db/models.py]

`delete:` Stale session docs (`features-and-fixes`, “WebDAV not started”). README + AGENTS.md. [PLAN.md, PROGRESS.md, HANDOFF.md]

`delete:` Cache methods never called from `Service` (`prune_orphans`/`rebuild_from_disk`/`clear_all`/`free_bytes`/`disk_free_bytes`). Nothing. [file_provider/cache.py]

`delete:` `/peek` + `FileProviderClient.peek` — dashboard uses `/tracks`. Nothing. [file_provider/api/main.py, provider/client.py]

`delete:` `FileProviderClient.health` — compose hits `/health` with urllib. Nothing. [provider/client.py]

`delete:` FUSE-era gdrive watchdog. WebDAV sidecar is the mount now. [scripts/gdrive-mount-watchdog.sh]

`delete:` Second rclone watcher; compose `rclone-guard` already loops `rclone-serve`. [scripts/rclone-webdav-watchdog.sh]

`delete:` `PlaybackSnapshot` / `snapshot()` — tests only. Nothing. [bot/state.py]

`delete:` `db.connect()` shim — nobody calls it. `Database(path)`. [db/database.py]

`delete:` `commands.recent()` — no dashboard UI. Nothing. [dashboard/commands.py]

`delete:` Unused deps `authlib` (auth.py: “no authlib”) and `python-dotenv` (never imported; compose injects env). Drop from requirements.txt.

`yagni:` `_LazyApp` proxy. `uvicorn --factory dashboard.main:create_app`. [dashboard/main.py]

`yagni:` `HeartbeatHttpClient` Protocol, one duck type. Annotate `httpx.AsyncClient`. [bot/gatus_heartbeat.py]

`yagni:` `ElapsedClock` beside `RadioClock`. Seek from `radio.position()`. [bot/player.py]

`yagni:` `SessionUser.is_admin` always `True`. Delete the property. [dashboard/auth.py]

`yagni:` `CACHE_PATH` / `CACHE_MAX_GB` env aliases — bot never reads them; provider uses `FILE_PROVIDER_*`. Drop. [.env.example, docker-compose.yml]

`shrink:` Four duration formatters. One `format_hms` in `dashboard/queries.py`. [bot/commands.py, bot/milestones.py, bot/main.py]

`shrink:` `FileProviderClient.next`/`previous`/`jump` copy the same JSON parse. `_post_track(path)`. [provider/client.py]

`native:` CI file not in `.github/workflows`. Move or delete. [ci/github-actions.yml]

net: ~-900 lines, -4 deps possible.
