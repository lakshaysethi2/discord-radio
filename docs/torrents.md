# Torrent library

The file-provider includes a torrent backend powered by the headless `aria2c`
client. The provider image installs aria2 and starts a loopback-only JSON-RPC
client automatically; aria2's RPC port should not be published to the host.

## Configuration

The default provider order is `local,torrent`. The important settings are:

```dotenv
FILE_PROVIDER_TORRENT_ENABLED=1
FILE_PROVIDER_TORRENT_DATA_PATH=/data/torrents
FILE_PROVIDER_TORRENT_RPC_PORT=6800
FILE_PROVIDER_TORRENT_RPC_SECRET=
```

`FILE_PROVIDER_ORDER` can include `torrent` alongside `local`, `archive`, and
`telegram`. Set `FILE_PROVIDER_TORRENT_ENABLED=0` to disable the backend. The
provider database stores aria2 GIDs and file-selection state in the existing
`FILE_PROVIDER_DB_PATH` database. Downloaded torrent data is kept separately
from the LRU playback cache.

The provider rejects remote aria2 RPC URLs by default. Keep
`FILE_PROVIDER_TORRENT_ALLOW_REMOTE_RPC=0` unless an externally managed,
properly secured aria2 endpoint is intentional. The default safety limits are a
16 MiB `.torrent` upload and 10 GiB per torrent; adjust
`FILE_PROVIDER_TORRENT_MAX_UPLOAD_MB` and `FILE_PROVIDER_TORRENT_MAX_SIZE_GB`
only when the host has enough disk capacity. `FILE_PROVIDER_TORRENT_ALLOWED_EXTENSIONS`
controls which file extensions can be added to the radio playlist. The built-in
list includes common containers such as MP3, M4A, FLAC, MKA, MP4, MKV, WebM,
M2TS, MTS, VOB, and OGV. If a legitimate format is missing, add its extension
to this setting and restart the file-provider. The dashboard also shows every
file returned by aria2 and provides a clearly marked **Use as media** override
for an administrator who has verified an unusual or extensionless file really
contains playable audio/video.

## Local development

The production Docker image starts aria2 automatically. For a local provider
process, install aria2 with the host package manager and start a loopback-only
daemon using the same data directory:

```bash
mkdir -p data/torrents data
: > data/aria2.session
aria2c --enable-rpc=true --rpc-listen-all=false --rpc-listen-port=6800 \
  --dir="$PWD/data/torrents" \
  --input-file="$PWD/data/aria2.session" \
  --save-session="$PWD/data/aria2.session"
```

The service and aria2 are network-light but disk-heavy: aria2 can use up to
`FILE_PROVIDER_TORRENT_MAX_SIZE_GB` per torrent, while the playback cache has
its separate `FILE_PROVIDER_CACHE_MAX_GB` limit. Keep both directories on
persistent storage and monitor free space.

## Dashboard workflow

1. Sign in to the dashboard and open **Torrents**.
2. Add a magnet link or upload a `.torrent` file.
3. Wait for aria2 to discover metadata and download the desired file. While a magnet is resolving, aria2 may report one zero-byte `[METADATA]...` pseudo-file; that is not the real media yet.
4. Click **Add to playlist** for each playable audio/video file. Individual
   files are exposed as normal file-provider tracks and are immediately
   available on the Queue page; video containers are passed through FFmpeg's
   audio-only playback path.
5. Use **Pause**, **Resume**, or **Remove** to manage the torrent. Removing a
   torrent also removes its selected files from the radio playlist and clears
   their playback-cache entries.

The dashboard never talks directly to aria2. It sends authenticated admin
requests to the file-provider API, and the provider validates torrent file
paths before caching them. Files that are not complete cannot be fetched for
playback, even if an administrator has enabled them in the playlist.

## Recovery

If aria2 is restarted, the provider reuses its session file at
`FILE_PROVIDER_TORRENT_DATA_PATH/../aria2.session` and keeps the dashboard
index in `FILE_PROVIDER_DB_PATH`. If the two become inconsistent, stop the
file-provider, back up `data/provider.db`, `data/aria2.session`, and
`data/torrents`, then remove only the stale torrent rows with SQLite or use the
Dashboard **Remove** action after restarting aria2. Re-add a magnet or upload
its `.torrent` file if aria2 no longer has the download session. Do not delete
`data/torrents` unless the downloaded data itself can be discarded.

## Provider API

The torrent management endpoints are intentionally on the internal provider
service and are not authenticated themselves; deploy the provider behind the
private Docker network as in `docker-compose.yml`:

- `GET /torrents`
- `POST /torrents/magnet` with `{"magnet":"magnet:?…"}`
- `POST /torrents/file` as multipart form field `file`
- `POST /torrents/{gid}/files/{index}` with `{"enabled":true|false}`
- `POST /torrents/{gid}/action` with `{"action":"pause"|"resume"|"remove"}`
