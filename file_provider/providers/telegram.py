"""Telegram (MTProto / Telethon) backend.

Adapted from the hawkins-tv reference (StringSession, disk cache, per-thread
event loop). Differences vs. the reference:

* We download audio for a Discord *voice* stream instead of proxying HTTP
  video, so we always fetch the whole file to disk before returning it —
  FFmpeg will read it locally with ``-ss`` for pause/resume.
* We accept both ``audio`` and ``document`` messages whose mime starts with
  ``audio/`` (voice, m4a, mp3, ogg, opus, flac).
* We don't do the SQLite-session migration dance; on first run the operator
  authenticates interactively and the session string lands on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from file_provider.providers.base import BaseProvider, ProviderFetchError, ProviderTrack

log = logging.getLogger(__name__)


def _run_async(coro):
    """Run a coroutine in a fresh event loop (safe from any thread)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TelegramProvider(BaseProvider):
    name = "telegram"

    def __init__(
        self,
        api_id: str,
        api_hash: str,
        channel_id: str,
        session_path: Path,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.channel_id = channel_id
        self.session_path = session_path

    # ------------------------------------------------------------- helpers
    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.channel_id)

    def _load_session(self) -> str:
        if self.session_path.exists():
            return self.session_path.read_text().strip()
        return ""

    def _save_session(self, session_string: str) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions — the session grants full account access.
        self.session_path.write_text(session_string)
        with contextlib.suppress(OSError):  # non-posix
            os.chmod(self.session_path, 0o600)

    def _build_client(self):
        # Imports are local so tests + docs generation don't require telethon.
        from telethon import TelegramClient  # type: ignore[import-not-found]
        from telethon.sessions import StringSession  # type: ignore[import-not-found]

        return TelegramClient(StringSession(self._load_session()), int(self.api_id), self.api_hash)

    # ------------------------------------------------------------ scanning
    def list_tracks(self) -> list[ProviderTrack]:
        if not self.is_configured():
            log.warning("telegram provider not configured; skipping scan")
            return []
        return _run_async(self._scan())

    async def _scan(self) -> list[ProviderTrack]:
        from telethon.sessions import StringSession  # type: ignore[import-not-found]

        client = self._build_client()
        await client.start()
        try:
            self._save_session(StringSession.save(client.session))
            entity = await client.get_entity(int(self.channel_id))
            tracks: list[ProviderTrack] = []
            seen: set[str] = set()
            async for msg in client.iter_messages(entity, limit=None):
                doc = self._extract_audio(msg)
                if doc is None:
                    continue
                source_ref = str(msg.id)
                if source_ref in seen:
                    continue
                seen.add(source_ref)
                title = self._title_for(msg, doc)
                tracks.append(
                    ProviderTrack(
                        title=title,
                        source_ref=source_ref,
                        duration_seconds=self._duration(doc),
                        size_bytes=int(getattr(doc, "size", 0) or 0),
                    )
                )
            # Preserve message id order (chronological, oldest last => reverse).
            tracks.sort(key=lambda t: int(t.source_ref))
            return tracks
        finally:
            await client.disconnect()

    @staticmethod
    def _extract_audio(msg):
        doc = getattr(msg, "audio", None) or getattr(msg, "voice", None)
        if doc is not None:
            return doc
        doc = getattr(msg, "document", None)
        if doc is not None and (getattr(doc, "mime_type", "") or "").startswith("audio/"):
            return doc
        return None

    @staticmethod
    def _title_for(msg, doc) -> str:
        # Prefer caption, then filename attribute, then a generic label.
        caption = (getattr(msg, "message", "") or "").strip()
        if caption:
            title = caption.splitlines()[0][:200]
        else:
            title = ""
            for attr in getattr(doc, "attributes", []):
                fn = getattr(attr, "file_name", None)
                if fn:
                    title = fn
                    break
        if not title:
            title = f"Track {msg.id}"
        if "." in title:
            # Strip extension for readability.
            title = title.rsplit(".", 1)[0]
        return title

    @staticmethod
    def _duration(doc) -> int:
        for attr in getattr(doc, "attributes", []):
            d = getattr(attr, "duration", None)
            if d:
                return int(d)
        return 0

    # ------------------------------------------------------------ fetching
    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        if not self.is_configured():
            raise ProviderFetchError("telegram provider not configured")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path
        try:
            _run_async(self._download(source_ref, target_path))
        except Exception as exc:  # broad on purpose — Telethon raises many types
            # Clean up half-written file.
            partial = target_path.with_suffix(target_path.suffix + ".part")
            for p in (partial, target_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
            raise ProviderFetchError(f"telegram download failed: {exc}") from exc
        return target_path

    async def _download(self, source_ref: str, target_path: Path) -> None:
        client = self._build_client()
        await client.start()
        try:
            entity = await client.get_entity(int(self.channel_id))
            msg = await client.get_messages(entity, ids=int(source_ref))
            if not msg:
                raise ProviderFetchError(f"message {source_ref} not found")
            doc = self._extract_audio(msg)
            if doc is None:
                raise ProviderFetchError(f"no audio in message {source_ref}")

            partial = target_path.with_suffix(target_path.suffix + ".part")
            with open(partial, "wb") as f:
                async for chunk in client.iter_download(doc, chunk_size=64 * 1024):
                    f.write(chunk)
            os.replace(partial, target_path)
        finally:
            await client.disconnect()
