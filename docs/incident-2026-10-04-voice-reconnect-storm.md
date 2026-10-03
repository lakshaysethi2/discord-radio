# Incident — bot leaves/rejoins voice ~120×/hour (2026-10-04)

Investigated read-only on prod (`tvbot-bot`). No code changed.

## Symptom

Bot repeatedly disconnects and rejoins the DND voice channel. Cycles of ~30s,
starting whenever a listener is present (today 13:59 UTC onward, ongoing; also
Oct 2 15:00–16:00). Listeners hear the same ~25s slice of audio on repeat.

## Evidence

Every voice session lives **24.4–29.3s**, never longer (216 sessions sampled
over 12h: min 24.4 / p50 26.9 / max 29.3). Cycle from `docker logs tvbot-bot`:

```
15:42:00,840 Voice connection complete.
15:42:00,857 silence recovery: restarted webdav_c6a63726aa74b687
15:42:25,910 Reconnect was unsuccessful, disconnecting from voice normally...
15:42:26,135 guild ...: bot was disconnected from voice channel by Discord
15:42:30,863 voice disconnected or uninitialized — reconnecting
```

- Discord closes the voice WS with the "by force" code
  (`site-packages/discord/voice_state.py:698`, codes 4014/4022).
- discord.py's internal `_potential_reconnect()` fails → normal disconnect.
- Our 5s watchdog `_silence_recovery_loop` (`bot/main.py:1352`) reconnects ~4.5s later.
- ≈120 leave/rejoin cycles per hour.

## Ruled out

- **Duplicate bot instance / token fight**: only `tvbot-bot` runs `bot/main.py`
  (PID map checked against every running container).
- **Host/network**: other voice bots on the box show 0 reconnect events
  (`acim-discord`, `prayer-bot`, `docbot`, `sedona-surrender-bot`);
  ICMP to `c-syd*.discord.media` 0% loss @1.3ms;
  conntrack 1896/262144; disk 62% used, 8% inodes.
- **Gateway/ratelimit**: no 4021, no missed-heartbeat
  (`Disconnected from voice... Reconnecting in Xs`), gateway keeps RESUMING fine.

## Leading hypothesis

Host I/O saturation starves ffmpeg so audio feeding goes marginal and Discord
ends the session:

- `/proc/pressure/io`: `full avg10=35%` (35% of the last 10s *all* tasks stalled)
- iowait 35–41%, load 5.7 on 4 vCPU, ~900MB free RAM, no swap
- current track is the largest item in the cache:
  `webdav_c6a63726aa74b687.audio` = **2.26 GB**, `duration_seconds: 0`,
  `has_video: true` — re-spawned onto every cycle, so a multi-GB file is
  re-read under full IO stall

Needs one confirmatory datapoint (event-loop lag metric or an ffmpeg stall log)
to be certain.

## Code bug that keeps the storm alive

`recover_silent_playback()` (`bot/main.py:276-321`) runs on every reconnect
cycle and calls `radio.reset(0)` + `st.player.start(nxt)`. The shared cursor is
slammed back to 0 every ~30s, so the track restarts from the top and the radio
never advances. Because playback "recovers" instantly, `is_radio_healthy()`
(`bot/gatus_heartbeat.py:64`) never flags the outage — the recovery loop hides
the failure instead of surfacing it.

## Verdict

- Leave/rejoin cause: **Discord-side voice-session termination at <30s while the
  channel is occupied** — not gateway, not host network, not a duplicate bot.
- Why it never escapes: `recover_silent_playback` resetting the cursor to 0 on
  each reconnect.

## Proposed follow-ups (not applied)

1. Stop resetting the shared cursor in silence recovery; restart only after the
   10s silence threshold, keeping the current position.
2. Investigate the 2.26 GB `webdav_c6a63726aa74b687` track.
3. Cache is at 96% of its 10 GiB cap (`/health`: cache_bytes 10304036231 /
   cache_max_bytes 10737418240) — prune policy needs a look.
4. file-provider prefetch failures for dead rows:
   `Inbox/old-hadalready/*.mp3 → HTTP 404` (webdav) and
   `Hawkins_Lectures_transcoded_actual_files/*.mp3 → HTTP 500` (archive.org).
