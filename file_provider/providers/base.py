"""Abstract provider interface.

Every backend (local, telegram, gdrive, ...) implements this. The rest of the
service doesn't care what the backend is — it just calls list_tracks,
ensure_cached, and is_configured.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ProviderTrack:
    """A track as reported by a backend during a playlist scan.

    ``source_ref`` is opaque to the rest of the app — it's the pointer this
    provider will use later to fetch the file (a Telegram message id, a
    filesystem path, a GDrive file id, ...). We combine (provider, source_ref)
    into a stable ``track_id``.
    """

    title: str
    source_ref: str
    duration_seconds: int = 0
    size_bytes: int = 0

    def track_id(self, provider_name: str) -> str:
        # Deterministic short id: <provider>_<sha1-16>.
        h = hashlib.sha1(f"{provider_name}:{self.source_ref}".encode()).hexdigest()[:16]
        return f"{provider_name}_{h}"


class ProviderFetchError(RuntimeError):
    """Raised by ensure_cached when the backend can't deliver the file."""


@runtime_checkable
class BaseProvider(Protocol):
    """Structural protocol so tests can supply plain classes."""

    name: str

    def list_tracks(self) -> list[ProviderTrack]:
        """Return the full ordered playlist for this backend."""
        ...

    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        """Ensure the file at source_ref exists at target_path.

        Returns the path that actually holds the file. Raises
        ProviderFetchError on failure.
        """
        ...

    def is_configured(self) -> bool:
        """True if this provider has all env/config it needs to run."""
        ...
