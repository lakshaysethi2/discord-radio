# Dashboard setup (Discord OAuth2)

The dashboard uses **Discord OAuth2** for admin authentication. Login is
restricted to a whitelist of Discord user ids that you configure yourself.

## 1. Create the OAuth2 app

The bot and dashboard can share **the same Discord Application**.

1. Go to <https://discord.com/developers/applications>.
2. Open your existing bot application (or create a new one just for the
   dashboard, whichever you prefer).
3. **OAuth2 → General**:
   * Copy **Client ID** → `DISCORD_CLIENT_ID`
   * Reset & copy **Client Secret** → `DISCORD_CLIENT_SECRET`
   * Add a **Redirect** URL matching your public dashboard host, e.g.:
     ```
     https://tv.example.com/callback
     ```
     For local dev: `http://localhost:8000/callback`
   * Save.

## 2. Set env vars

```env
DISCORD_CLIENT_ID=1234567890
DISCORD_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxx
DISCORD_REDIRECT_URI=https://tv.example.com/callback

# Comma-separated Discord user ids allowed to log in as admin.
# Find your id: enable Developer Mode in Discord → right-click your name → Copy User ID.
ADMIN_USER_IDS=111111111111111111,222222222222222222

# Session cookie signing key. Generate one:
#   openssl rand -hex 32
DASHBOARD_SECRET_KEY=paste-your-random-hex-here
```

## 3. Put HTTPS in front

Discord OAuth2 requires the redirect URL to be reachable and, in production,
HTTPS. Use Cloudflare, nginx, Caddy, or Traefik as the TLS terminator; point
it at the dashboard container on port 8000.

The dashboard trusts `X-Forwarded-*` headers when started with
`--proxy-headers` (already set in the compose file), so cookies get the
`Secure` flag correctly.

## 4. Test the login

Visit `https://tv.example.com/` — you'll be redirected to `/login`, then to
Discord's authorize page, then back to `/callback`. On success:

* If your user id is in `ADMIN_USER_IDS`, you land on `/dashboard`.
* Otherwise you see a friendly "not on the admin whitelist" page.

## Session cookies

* Cookie name: `tvbot_session`.
* Signed (not encrypted) using `DASHBOARD_SECRET_KEY` via
  `itsdangerous.URLSafeSerializer`.
* Lifetime: 7 days (see `dashboard.auth.COOKIE_MAX_AGE`).
* `HttpOnly`, `SameSite=lax`. `Secure` is set automatically when the request
  arrives over HTTPS.

## Rotating the signing key

If you change `DASHBOARD_SECRET_KEY`, all existing sessions instantly become
invalid — users just log in again. Do it any time you suspect leakage.
