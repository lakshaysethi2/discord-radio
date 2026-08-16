#!/usr/bin/env bash
# gdrive mount watchdog for the discord-radio rclone FUSE mount.
#
# Failure signature (2026-08-16, and before): the google_drive FUSE mount
# dies, its stale "Transport endpoint is not connected" entry stays behind,
# containers holding bind mounts (qbittorrent, copyparty) keep it busy, and
# rclone-gdrive.service crash-loops forever with no human noticing until the
# radio goes silent and gatus/telegram catch it.
#
# This script detects the dead mount and breaks the loop in order:
#   1. stop the crash-looping rclone-gdrive.service
#   2. unmount the stale FUSE entry (lazy fallback)
#   3. restart the containers whose bind mounts hold the mountpoint
#   4. start rclone-gdrive.service again
#   5. verify the mount answers
# Every step is logged; exits 0 when the mount is healthy.
#
# Run every minute from cron. Idempotent and safe to re-run.

set -u

MOUNT=/home/ubuntu/mnt/google_drive
SERVICE=rclone-gdrive.service
LOG=/home/ubuntu/.cache/rclone/gdrive-mount-watchdog.log
HOLDERS=(qbittorrent copyparty)

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

if ls "$MOUNT"/ >/dev/null 2>&1; then
    exit 0
fi

log "MOUNT DEAD - starting recovery"

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl stop "$SERVICE" 2>>"$LOG" || true
    log "stopped $SERVICE"
fi

if grep -q "$MOUNT" /proc/self/mounts 2>/dev/null; then
    if ! fusermount -u "$MOUNT" 2>>"$LOG"; then
        log "fusermount -u failed, lazy unmount"
        umount -l "$MOUNT" 2>>"$LOG" || true
    fi
    log "cleared stale mount entry"
fi

for c in "${HOLDERS[@]}"; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
        docker restart "$c" >>"$LOG" 2>&1 || log "docker restart $c FAILED"
        log "restarted container $c"
    fi
done

if ! systemctl start "$SERVICE" 2>>"$LOG"; then
    log "start $SERVICE FAILED"
    exit 1
fi
log "started $SERVICE"

for i in 1 2 3 4 5 6; do
    if ls "$MOUNT"/ >/dev/null 2>&1; then
        log "RECOVERED: mount answers"
        exit 0
    fi
    sleep 10
done

log "STILL DEAD after recovery - manual intervention required"
exit 1
