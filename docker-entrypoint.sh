#!/bin/bash
set -e

# Ensure container mount directories exist
mkdir -p /data /cache /media 2>/dev/null || true

# If running as root inside container, fix ownership/permissions on mounted volumes
# and step down to APP_USER (or UID 1000).
#
# Note: /media is NOT recursively chowned — it may be a large read-only mount
# (e.g. the rclone FUSE drive mount) and walking it would hang the container.
# Files there are already presented with the right ownership by the mount.
if [ "$(id -u)" = '0' ]; then
    TARGET_USER="${APP_USER:-1000}"
    chown -R "$TARGET_USER:$TARGET_USER" /data /cache 2>/dev/null || true
    chmod 775 /data /cache /media 2>/dev/null || true
    exec gosu "$TARGET_USER" "$@"
fi

exec "$@"
