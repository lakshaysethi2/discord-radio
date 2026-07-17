"""Bot-side client for the File Provider service.

The bot never touches Telegram/GDrive/YouTube directly — it asks the file
provider over HTTP: "give me the current track" / "give me the next track".
The provider is responsible for backend selection, caching, and pre-fetch.

See blueprint §4.1 for the JSON contract.
"""

from provider.client import FileProviderClient, ProviderError, ProviderUnavailable, TrackResponse

__all__ = ["FileProviderClient", "ProviderError", "ProviderUnavailable", "TrackResponse"]
