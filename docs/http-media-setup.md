# HTTP Media Provider Setup

The HTTP media backend lets you add a **private HTTP media library** to
discord-radio's playlist. It works with [rclone `serve http`][rclone-serve]
and nginx autoindex — any server that produces an HTML directory listing with
`<a href="...">` links pointing to playable media files.

## How it works

1. Set `HTTP_MEDIA_BASE_URL` to the root URL of your HTTP directory listing.
2. (Optional) Set `HTTP_MEDIA_USER` / `HTTP_MEDIA_PASSWORD` for HTTP Basic auth.
3. Add `http` to `FILE_PROVIDER_ORDER` (e.g., `FILE_PROVIDER_ORDER=local,http,torrent`).
4. The provider scans the directory recursively for playable audio/video files.
5. Files are downloaded on demand into the shared LRU cache when the playlist
   reaches them.

## Configuration

| Env var | Required | Default | Description |
|---|---|---|---|
| `HTTP_MEDIA_BASE_URL` | Yes (to enable) | (empty) | Base URL of the HTTP directory listing. Provider is inactive when unset. |
| `HTTP_MEDIA_USER` | No | (empty) | HTTP Basic auth username. |
| `HTTP_MEDIA_PASSWORD` | No | (empty) | HTTP Basic auth password. Never logged. |
| `HTTP_MEDIA_TIMEOUT` | No | `60` | HTTP request timeout in seconds. |

### Example (production, using placeholders)

```env
# .env (replace placeholders with real values only in the live .env file)
HTTP_MEDIA_BASE_URL=https://media.example.invalid/library
HTTP_MEDIA_USER=myuser
HTTP_MEDIA_PASSWORD=mypassword
HTTP_MEDIA_TIMEOUT=120
FILE_PROVIDER_ORDER=local,http,archive,torrent
```

## Backend compatibility

| Backend | Works? | Notes |
|---|---|---|
| **rclone `serve http`** | Yes | Produces clean HTML directory listings with `<a href>` links. Recursive crawl works. |
| **nginx autoindex** | Yes | Default HTML listing format works the same way. `fancyindex` module also works as long as it produces `<a href>` links. |
| **Apache mod_autoindex** | Likely | Similar HTML format. Test if your Apache version produces parseable links. |
| **S3 static website** | No | S3 does not produce HTML index pages by default. Use rclone to mount S3 and serve via `serve http`. |

## Security notes

- **Never commit the real base URL or credentials** to the repository.
  `.env` is git-ignored by default.
- The provider never logs password values.
- Passwords are transmitted over HTTP Basic auth (base64-encoded, not encrypted).
  **Always use HTTPS** for the media library in production.
- The provider follows redirects — use a CDN / signed URL in front if needed.

## Troubleshooting

**Provider not showing any tracks:**
- Verify `HTTP_MEDIA_BASE_URL` is set and reachable from the file-provider container.
- Check the file-provider logs: `make logs-provider | grep http_media`
- Manually visit the URL to verify it shows an HTML directory listing.
- Check that `http` is in `FILE_PROVIDER_ORDER`.

**Downloads fail:**
- Check the file-provider logs for HTTP error codes.
- Verify credentials if using Basic auth.
- Ensure the media library server is reachable from the file-provider container
  (Docker networking, firewall rules).

[rclone-serve]: https://rclone.org/commands/rclone_serve_http/
