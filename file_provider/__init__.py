"""File Provider service.

A separate FastAPI service that owns backend selection (Telegram, local FS,
future YouTube/GDrive/Torrent), the on-disk cache, and the playlist cursor.
The bot only talks to it over HTTP (see `provider.client`).

See blueprint §4.1.
"""
