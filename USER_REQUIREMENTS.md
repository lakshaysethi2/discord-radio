# User Requirements

## Slash Commands

### `/current` — Currently Playing
- Accessible to any user in a server where the bot is active.
- Always defers the Discord interaction first so the 3-second window is never missed,
  even when the file-provider is slow. Uses `followup.send` for the actual response.
- Shows the currently playing track with:
  - Track title
  - Duration (formatted as Xh Ym)
  - Progress bar or elapsed/total time
  - Pause indicator (⏸️) when paused
  - Track number in playlist
  - Current watcher count (scoped to the server)
- Shows a helpful message when nothing is playing.
- Gracefully handles provider errors (shows error message, not a crash).

### `/leaderboard` — Listening Leaderboard
- Shows all-time top 10 listeners ranked by total listening time.
- Response is **ephemeral** (only visible to the user who invoked it).
- Response is **dismiss-able** (Discord ephemeral messages are dismissible by default).
- Shows medal emoji for top 3 (🥇🥈🥉).
- Shows rank number, display name (server_nickname or username), and formatted time.
- Usernames are markdown-escaped to prevent formatting abuse.
- Shows a helpful message when no data exists yet.
- Footer indicates "All-time total listening time".

## File Provider Backends

### HTTP Media Provider (private library)
- Activated by setting `HTTP_MEDIA_BASE_URL` env var (inactive when unset).
- Reads optional `HTTP_MEDIA_USER` and `HTTP_MEDIA_PASSWORD` for HTTP Basic auth.
- Scans rclone `serve http` or nginx autoindex HTML directory listings recursively
  for playable audio/video files.
- Follows HTTP redirects.
- Downloads files on demand into the shared LRU cache.
- Credentials are never logged.

### Archive.org Provider (public / mirror)
- Supports configurable base URLs via `ARCHIVE_ORG_BASE_URLS` (comma-separated,
  defaults to `https://archive.org`).
- Optional `ARCHIVE_ORG_HTTP_USER` / `ARCHIVE_ORG_HTTP_PASSWORD` for Basic auth
  on mirror hosts.
- Multiple archive.org items can be configured comma-separated.
- Only `source: original` files are included in the playlist (derivatives excluded).

### Other Providers
- **Local** (`local`): Scans a local directory recursively.
- **Torrent** (`torrent`): Manages aria2 torrent downloads.
- **Telegram** (`telegram`): Downloads from Telegram channels via MTProto.

## Robustness

### Lock-free cached track access
- The `get_by_id` endpoint for already-cached tracks does not block behind
  unrelated downloads — cached tracks are returned immediately without waiting
  on per-track fetch locks held by other tracks.

### /current defer safety net
- `/current` always ACKs Discord within the interaction window by calling
  `interaction.response.defer()` before any provider HTTP call.
- The actual response is sent via `interaction.followup.send()`.

## Implementation Details
- Slash commands are registered via `discord.app_commands.CommandTree` on the existing `discord.Client`.
- Commands sync globally on first `on_ready`.
- Commands are guarded against double-registration on reconnect.
- Dependencies (DB, provider, state, radio clock, stations) are injected via closures for testability.
- Tests cover all command code paths without requiring a live Discord connection.
