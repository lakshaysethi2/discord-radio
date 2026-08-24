#!/usr/bin/env bash
# Keep the shared rclone WebDAV sidecar (rclone-serve) running.
#
# discord-radio talks to it as http://rclone-webdav:8081. If the container
# exits (OOM/SIGKILL, quota crash), file-provider 502s and the radio goes
# silent. Idempotent: no-op when already running and WebDAV answers.

set -u

NAME=rclone-serve
LOG=/home/ubuntu/.cache/rclone/rclone-webdav-watchdog.log
# Probe from a container already on discord-radio_default; host has no :8081.
PROBE_CONTAINER=tvbot-file-provider
PROBE_URL=http://rclone-webdav:8081/

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

running() {
    docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -qx true
}

webdav_ok() {
    docker exec "$PROBE_CONTAINER" python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('$PROBE_URL', timeout=8).status<500 else 1)" \
        >/dev/null 2>&1
}

if running && webdav_ok; then
    exit 0
fi

if ! running; then
    log "rclone-serve not running — docker start"
    if ! docker start "$NAME" >>"$LOG" 2>&1; then
        log "docker start $NAME FAILED"
        exit 1
    fi
    sleep 3
fi

for i in 1 2 3 4 5 6; do
    if running && webdav_ok; then
        log "RECOVERED: WebDAV answers"
        exit 0
    fi
    sleep 5
done

log "STILL DEAD after start — WebDAV not answering"
exit 1
