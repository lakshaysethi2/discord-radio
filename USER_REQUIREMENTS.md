# User Requirements

## Slash Commands

### `/current` — Currently Playing
- Accessible to any user in a server where the bot is active.
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

## Implementation Details
- Slash commands are registered via `discord.app_commands.CommandTree` on the existing `discord.Client`.
- Commands sync globally on first `on_ready`.
- Commands are guarded against double-registration on reconnect.
- Dependencies (DB, provider, state, radio clock, stations) are injected via closures for testability.
- Tests cover all command code paths without requiring a live Discord connection.
