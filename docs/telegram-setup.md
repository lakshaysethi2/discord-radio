# Telegram (MTProto) backend setup

The file-provider service can pull audio from a Telegram channel using
**Telethon** (MTProto client), just like the reference project
[`hawkins-tv`](https://gitlab.com/lakclawbot/hawkins-tv). Advantages over the
Bot API:

* No file-size limits (Bot API caps around 20 MB).
* Can scan the full channel message history.
* One-time interactive auth; a persistent `StringSession` is stored on disk.

## 1. Get an api_id + api_hash

1. Log in at <https://my.telegram.org/apps>.
2. Fill in a short app title (anything — e.g. "discord-tv"). Platform: "Other".
3. Copy the **api_id** (number) and **api_hash** (32-char string).

These identify **your Telegram account**, not the channel. Keep them secret.

## 2. Find your channel id

For public channels the id is `@channelname`, but Telethon needs the numeric
id. Two easy ways:

* Forward any message from the channel to `@userinfobot` — it reports the
  numeric channel id.
* Open the channel on <https://web.telegram.org> and copy the id from the URL
  (`#-100XXXXXXXXXX`).

Channel ids for supergroups / broadcast channels look like
`-1001234567890` (13 digits with a `-100` prefix).

## 3. Configure `.env`

```env
FILE_PROVIDER_ORDER=telegram
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_CHANNEL_ID=-1001234567890
```

## 4. First-run interactive auth

The first time the file-provider starts, Telethon will prompt for your phone
number and a login code (and 2FA password if you have one enabled). After
that, a `telethon.session.txt` file lands next to the provider DB and is
reused on every subsequent start — no more prompts.

To trigger the interactive flow inside Docker:

```bash
docker compose run --rm file-provider python -c "
from file_provider.config import load
from file_provider.providers.telegram import TelegramProvider
c = load()
TelegramProvider(c.telegram_api_id, c.telegram_api_hash,
                 c.telegram_channel_id, c.telethon_session_path()).list_tracks()
"
```

Enter your phone (`+64...`), the code Telegram texts you, and (if applicable)
your 2FA password. Once the session file is written, restart with
`docker compose up -d`.

## 5. Populate the playlist

Once authenticated:

```bash
curl -X POST http://localhost:8001/refresh
```

This scans the channel and populates the file-provider DB. Files are
downloaded lazily (and pre-fetched one ahead) as the bot plays them.

## Security notes

* The session file (`telethon.session.txt`) grants **full account access**.
  Treat it as a secret. The provider container writes it with `0600`
  permissions and it's in `.gitignore`.
* Never share `TELEGRAM_API_HASH` — it identifies your api_id.
* If you suspect compromise, revoke the session from Telegram's
  "Devices" screen.
