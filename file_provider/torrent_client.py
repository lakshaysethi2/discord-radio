"""aria2-backed torrent management for the file provider.

The file provider deliberately talks to aria2 over its loopback JSON-RPC
interface instead of implementing BitTorrent itself. aria2 is a mature,
headless torrent client with magnet links, .torrent files, resumable downloads,
DHT/PEX and per-file status. This module owns the small amount of application
state aria2 does not know about: which files an administrator has enabled in
the radio playlist.

The class is synchronous because the provider service is synchronous internally
and already serializes playlist/cache work. ``Aria2RpcClient`` is kept separate
so tests and installations with an externally managed aria2 daemon can inject a
fake RPC client.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import httpx

from file_provider.db import ProviderDB
from file_provider.media_types import PLAYABLE_EXTS
from file_provider.providers.base import ProviderFetchError

log = logging.getLogger(__name__)


class TorrentClientError(RuntimeError):
    """The torrent client rejected a request or returned malformed data."""


class TorrentClientUnavailable(TorrentClientError):
    """aria2 is not installed, not running, or cannot be reached."""


class TorrentSecurityError(TorrentClientError):
    """The torrent client configuration would expose or misuse aria2."""


class TorrentSizeLimitError(TorrentClientError):
    """A torrent exceeds the configured aggregate download size limit."""


def is_metadata_path(path: str) -> bool:
    """True for aria2's temporary magnet-metadata pseudo-file."""
    return Path(path).name.upper().startswith("[METADATA]")


class RpcClient(Protocol):
    def call(self, method: str, params: list[Any] | None = None) -> Any: ...


def validate_rpc_url(url: str, *, allow_remote: bool = False) -> None:
    """Reject remote aria2 endpoints unless the operator opts in explicitly."""
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise TorrentSecurityError("torrent RPC URL must be an http(s) URL with a hostname")
    if not allow_remote and host.lower().rstrip(".") not in {"localhost", "127.0.0.1", "::1"}:
        raise TorrentSecurityError(
            "remote torrent RPC is disabled; use localhost or set "
            "FILE_PROVIDER_TORRENT_ALLOW_REMOTE_RPC=1 explicitly"
        )


class Aria2RpcClient:
    """Small JSON-RPC 2.0 client for aria2."""

    def __init__(
        self,
        url: str,
        *,
        secret: str = "",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._request_id = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._request_id += 1
        rpc_params = list(params or [])
        if self.secret:
            rpc_params.insert(0, f"token:{self.secret}")
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": rpc_params,
        }
        try:
            response = self._client.post(self.url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise TorrentClientError(
                f"aria2 RPC {method} returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.TransportError as exc:
            raise TorrentClientUnavailable(f"aria2 RPC {method} failed: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TorrentClientError(
                f"aria2 RPC {method} returned invalid JSON: {response.text[:500]}"
            ) from exc
        if not isinstance(data, dict):
            raise TorrentClientError(f"aria2 RPC {method} returned a non-object")
        if data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                message = f"{error.get('code', '?')}: {error.get('message', 'unknown error')}"
            else:
                message = str(error)
            raise TorrentClientError(f"aria2 RPC {method}: {message}")
        return data.get("result")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


@dataclass(slots=True, frozen=True)
class TorrentFile:
    gid: str
    file_index: int
    path: str
    length: int
    completed_length: int
    selected: bool
    is_complete: bool
    playlist_enabled: bool
    media_override: bool
    is_metadata: bool
    playable: bool


    @property
    def progress_percent(self) -> float:
        if self.length <= 0:
            return 100.0 if self.is_complete else 0.0
        return round(min(100.0, self.completed_length * 100 / self.length), 1)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["progress_percent"] = self.progress_percent
        value["playable"] = self.playable
        value["name"] = Path(self.path).name or self.path
        return value


@dataclass(slots=True, frozen=True)
class Torrent:
    gid: str
    name: str
    info_hash: str | None
    source: str
    status: str
    total_length: int
    completed_length: int
    download_speed: int
    upload_speed: int
    error_code: str | None
    error_message: str | None
    files: tuple[TorrentFile, ...] = ()

    @property
    def progress_percent(self) -> float:
        if self.total_length <= 0:
            return 0.0
        return round(min(100.0, self.completed_length * 100 / self.total_length), 1)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["progress_percent"] = self.progress_percent
        value["files"] = [f.to_dict() for f in self.files]
        return value


class TorrentManager:
    """Own an aria2 process and persist dashboard-facing torrent state."""

    def __init__(
        self,
        db: ProviderDB,
        data_root: str | os.PathLike[str],
        *,
        rpc_url: str = "http://127.0.0.1:6800/jsonrpc",
        rpc_secret: str = "",
        rpc_port: int = 6800,
        binary: str = "aria2c",
        allow_remote_rpc: bool = False,
        max_size_bytes: int = 10 * 1024**3,
        max_upload_bytes: int = 16 * 1024**2,
        allowed_extensions: frozenset[str] | None = None,
        rpc: RpcClient | None = None,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.db = db
        self.data_root = Path(data_root)
        self.session_path = self.data_root.parent / "aria2.session"
        self.uploads_path = self.data_root / ".torrent_uploads"
        self.rpc_url = rpc_url
        self.allow_remote_rpc = allow_remote_rpc
        self.max_size_bytes = max(0, int(max_size_bytes))
        self.max_upload_bytes = max(1, int(max_upload_bytes))
        self.allowed_extensions = PLAYABLE_EXTS if allowed_extensions is None else allowed_extensions
        self.rpc_port = int(rpc_port)
        self.binary = binary
        self.rpc: RpcClient = rpc or Aria2RpcClient(rpc_url, secret=rpc_secret)
        self._rpc_owned = rpc is None
        self._process_factory = process_factory
        self._process: subprocess.Popen | None = None
        self._started = False
        self._last_start_error: str | None = None

    # --------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return self._started

    @property
    def last_start_error(self) -> str | None:
        return self._last_start_error

    def start(self) -> bool:
        """Connect to an existing daemon or start a private aria2 daemon."""
        if self._started:
            return True
        try:
            validate_rpc_url(self.rpc_url, allow_remote=self.allow_remote_rpc)
        except TorrentSecurityError as exc:
            self._last_start_error = str(exc)
            log.error(self._last_start_error)
            return False
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.touch(exist_ok=True)

        # This also supports a separately managed aria2 instance, useful when
        # the provider runs outside Docker.
        try:
            self.rpc.call("aria2.getVersion")
        except TorrentClientUnavailable:
            pass
        except TorrentClientError:
            # Authentication or another RPC-level error means the endpoint is
            # reachable; do not launch a second daemon over it.
            self._started = True
            return True
        else:
            self._started = True
            return True

        try:
            command = [
                self.binary,
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={self.rpc_port}",
                "--rpc-allow-origin-all=false",
                f"--dir={self.data_root}",
                f"--input-file={self.session_path}",
                f"--save-session={self.session_path}",
                "--save-session-interval=60",
                "--auto-save-interval=60",
                "--rpc-save-upload-metadata=true",
                "--file-allocation=none",
                "--continue=true",
                "--seed-time=0",
                "--seed-ratio=0",
                "--bt-stop-timeout=60",
                "--max-concurrent-downloads=3",
                "--auto-file-renaming=false",
                "--summary-interval=0",
            ]
            # The RPC URL's client is configured with the same secret. Do not
            # put an empty secret on the command line; aria2 treats that as a
            # real (and surprising) token value.
            if isinstance(self.rpc, Aria2RpcClient) and self.rpc.secret:
                command.append(f"--rpc-secret={self.rpc.secret}")
            self._process = self._process_factory(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._last_start_error = f"could not start {self.binary}: {exc}"
            log.warning(self._last_start_error)
            return False

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                error = ""
                if self._process.stderr is not None:
                    with contextlib.suppress(Exception):
                        error = self._process.stderr.read().decode(errors="replace")[-500:]
                self._last_start_error = f"{self.binary} exited during startup{(': ' + error) if error else ''}"
                log.warning(self._last_start_error)
                return False
            try:
                self.rpc.call("aria2.getVersion")
            except (TorrentClientUnavailable, TorrentClientError):
                time.sleep(0.1)
            else:
                self._started = True
                self._last_start_error = None
                log.info("started aria2 torrent client")
                return True
        self._last_start_error = "timed out waiting for aria2 JSON-RPC"
        log.warning(self._last_start_error)
        self.stop()
        return False

    def stop(self) -> None:
        if self._process is not None:
            with contextlib.suppress(Exception):
                if self._process.poll() is None:
                    self._process.terminate()
                    self._process.wait(timeout=5)
            with contextlib.suppress(Exception):
                if self._process.poll() is None:
                    self._process.kill()
            self._process = None
        self._started = False
        if self._rpc_owned and isinstance(self.rpc, Aria2RpcClient):
            self.rpc.close()

    def _require_ready(self) -> None:
        if not self._started:
            if not self.start():
                raise TorrentClientUnavailable(
                    self._last_start_error or "torrent client unavailable"
                )
            return

        # aria2 can disappear independently of the FastAPI process. A stale
        # `_started` flag must not turn the next dashboard operation into a
        # permanent 503; verify the RPC and restart our managed daemon once.
        try:
            self.rpc.call("aria2.getVersion")
        except TorrentClientUnavailable:
            log.warning("aria2 RPC stopped responding; attempting a restart")
            self._started = False
            if not self.start():
                raise TorrentClientUnavailable(
                    self._last_start_error or "torrent client unavailable"
                )

    # ------------------------------------------------------------- RPC helpers
    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _status_from_rpc(self, status: dict[str, Any]) -> dict[str, Any]:
        gid = str(status.get("gid") or "")
        files = status.get("files")
        if not isinstance(files, list):
            files = []
        total = self._int(status.get("totalLength"))
        completed = self._int(status.get("completedLength"))
        name = ""
        bittorrent = status.get("bittorrent")
        if isinstance(bittorrent, dict):
            info = bittorrent.get("info")
            if isinstance(info, dict):
                name = str(info.get("name") or "")
        if not name and files:
            name = Path(str(files[0].get("path") or "")).parts[0]
        return {
            "gid": gid,
            "name": name,
            "info_hash": status.get("infoHash"),
            "status": str(status.get("status") or "waiting").lower(),
            "total_length": total,
            "completed_length": completed,
            "download_speed": self._int(status.get("downloadSpeed")),
            "upload_speed": self._int(status.get("uploadSpeed")),
            "error_code": status.get("errorCode"),
            "error_message": status.get("errorMessage"),
            "files": files,
        }

    def sync_torrent(self, gid: str) -> Torrent:
        self._require_ready()
        try:
            raw_status = self.rpc.call(
                "aria2.tellStatus",
                [gid, [
                    "gid",
                    "status",
                    "totalLength",
                    "completedLength",
                    "downloadSpeed",
                    "uploadSpeed",
                    "errorCode",
                    "errorMessage",
                    "infoHash",
                    "bittorrent",
                ]],
            )
            raw_files = self.rpc.call("aria2.getFiles", [gid])
        except TorrentClientError:
            raise
        except Exception as exc:  # defensive for injected RPC implementations
            raise TorrentClientError(f"could not inspect torrent {gid}: {exc}") from exc

        if not isinstance(raw_status, dict):
            raise TorrentClientError(f"aria2 returned malformed status for {gid}")
        if not isinstance(raw_files, list):
            raw_files = []
        status = self._status_from_rpc(raw_status)
        if self.max_size_bytes and status["total_length"] > self.max_size_bytes:
            with contextlib.suppress(TorrentClientError):
                self.rpc.call("aria2.forceRemove", [gid])
            self.db.remove_torrent(gid)
            raise TorrentSizeLimitError(
                f"torrent is {status['total_length']} bytes, exceeding the "
                f"configured limit of {self.max_size_bytes} bytes"
            )
        if not status["name"] and raw_files:
            first_path = Path(str(raw_files[0].get("path") or ""))
            if not is_metadata_path(str(first_path)):
                try:
                    relative = first_path.resolve().relative_to(self.data_root.resolve())
                except ValueError:
                    relative = first_path
                status["name"] = relative.parts[0] if relative.parts else first_path.name
        status["source"] = ""
        previous = self.db.torrent(gid)
        if previous is not None:
            status["source"] = previous["source"]
        self.db.upsert_torrent(status)

        for item in raw_files:
            if not isinstance(item, dict):
                continue
            length = self._int(item.get("length"))
            done = self._int(item.get("completedLength"))
            path = str(item.get("path") or "")
            self.db.upsert_torrent_file(
                gid,
                {
                    "file_index": self._int(item.get("index")),
                    "path": path,
                    "length": length,
                    "completed_length": done,
                    "selected": str(item.get("selected", "true")).lower() == "true",
                    # aria2 exposes a zero-length [METADATA] pseudo-file while
                    # a magnet is still resolving. It is not a completed
                    # playable file.
                    "is_complete": (
                        not is_metadata_path(path) and length > 0 and done >= length
                    ),
                },
            )
        return self._torrent_from_db(gid)

    def _torrent_from_db(self, gid: str) -> Torrent:
        row = self.db.torrent(gid)
        if row is None:
            raise TorrentClientError(f"unknown torrent {gid}")
        files = tuple(
            TorrentFile(
                gid=gid,
                file_index=int(file["file_index"]),
                path=file["path"],
                length=int(file["length"] or 0),
                completed_length=int(file["completed_length"] or 0),
                selected=bool(file["selected"]),
                is_complete=bool(file["is_complete"]),
                playlist_enabled=bool(file["playlist_enabled"]),
                media_override=bool(file["media_override"]),
                is_metadata=is_metadata_path(file["path"]),
                playable=(not is_metadata_path(file["path"]))
                and (
                    bool(file["media_override"])
                    or Path(file["path"]).suffix.lower() in self.allowed_extensions
                ),
            )
            for file in self.db.torrent_files(gid)
        )
        return Torrent(
            gid=row["gid"],
            name=row["name"],
            info_hash=row["info_hash"],
            source=row["source"],
            status=row["status"],
            total_length=int(row["total_length"] or 0),
            completed_length=int(row["completed_length"] or 0),
            download_speed=int(row["download_speed"] or 0),
            upload_speed=int(row["upload_speed"] or 0),
            error_code=row["error_code"],
            error_message=row["error_message"],
            files=files,
        )

    def list_torrents(self) -> list[Torrent]:
        self._require_ready()
        out: list[Torrent] = []
        # aria2 keeps completed downloads in its result list. Re-syncing those
        # rows also refreshes per-file completion after a restart.
        for row in self.db.list_torrents():
            gid = row["gid"]
            try:
                torrent = self.sync_torrent(gid)
                if self._is_duplicate_error(torrent) and torrent.info_hash:
                    try:
                        out.append(self._finish_added(gid))
                    except TorrentClientError as exc:
                        log.warning("removed duplicate torrent %s: %s", gid, exc)
                else:
                    out.append(torrent)
            except TorrentSizeLimitError as exc:
                # The row and aria2 job were removed by sync_torrent.
                log.warning("removed oversized torrent %s: %s", gid, exc)
            except TorrentClientError as exc:
                log.debug("could not refresh torrent %s: %s", gid, exc)
                if self.db.torrent(gid) is not None:
                    out.append(self._torrent_from_db(gid))
        # A newly-added magnet may not have a DB row if the first status call
        # raced metadata acquisition; callers still get a stable response once
        # the next refresh runs.
        return out

    def add_magnet(self, magnet: str) -> Torrent:
        magnet = magnet.strip()
        if not magnet.lower().startswith("magnet:?"):
            raise TorrentClientError("only magnet links are accepted")
        self._require_ready()
        result = self.rpc.call("aria2.addUri", [[magnet], self._download_options()])
        gid = str(result or "")
        if not gid:
            raise TorrentClientError("aria2 did not return a download id")
        self.db.upsert_torrent({"gid": gid, "source": magnet, "status": "waiting"})
        return self._finish_added(gid)

    def add_torrent_file(self, content: bytes, filename: str = "upload.torrent") -> Torrent:
        if not content:
            raise TorrentClientError("the .torrent file is empty")
        if len(content) > self.max_upload_bytes:
            raise TorrentClientError(
                f"the .torrent upload exceeds the configured limit of {self.max_upload_bytes} bytes"
            )
        self._require_ready()
        encoded = base64.b64encode(content).decode("ascii")
        # aria2.addTorrent has a different signature from addUri:
        # (torrent, uris, options, position). The empty URI list is required
        # here; passing options as parameter 1 makes aria2 report "wrong type".
        result = self.rpc.call(
            "aria2.addTorrent", [encoded, [], self._download_options()]
        )
        gid = str(result or "")
        if not gid:
            raise TorrentClientError("aria2 did not return a download id")
        # aria2's session file is enough for most restarts, but retaining the
        # uploaded metadata lets an operator recover the torrent even when a
        # session was interrupted before its first autosave.
        digest = hashlib.sha256(content).hexdigest()
        self.uploads_path.mkdir(parents=True, exist_ok=True)
        upload_path = self.uploads_path / f"{digest}.torrent"
        if not upload_path.exists():
            upload_path.write_bytes(content)
        self.db.upsert_torrent(
            {"gid": gid, "source": str(upload_path), "status": "waiting"}
        )
        return self._finish_added(gid)

    def _sync_or_db(self, gid: str) -> Torrent:
        try:
            return self.sync_torrent(gid)
        except TorrentSizeLimitError:
            raise
        except TorrentClientError:
            # Metadata for a magnet is legitimately unavailable for a short
            # period. The dashboard can show the waiting row immediately.
            return self._torrent_from_db(gid)

    @staticmethod
    def _is_duplicate_error(torrent: Torrent) -> bool:
        return torrent.status == "error" and "already registered" in (
            torrent.error_message or ""
        ).lower()

    def _finish_added(self, gid: str) -> Torrent:
        """Index a newly added job and collapse duplicate-infohash errors."""
        torrent = self._sync_or_db(gid)
        if not self._is_duplicate_error(torrent) or not torrent.info_hash:
            return torrent

        existing = self.db.torrent_by_info_hash(torrent.info_hash, exclude_gid=gid)
        with contextlib.suppress(TorrentClientError):
            self.rpc.call("aria2.forceRemove", [gid])
        self.db.remove_torrent(gid)
        if existing is not None:
            try:
                return self.sync_torrent(existing["gid"])
            except TorrentClientError:
                return self._torrent_from_db(existing["gid"])
        raise TorrentClientError(
            f"torrent {torrent.info_hash} is already registered in aria2; "
            "remove the existing torrent or use the existing dashboard entry"
        )

    def _download_options(self) -> dict[str, str]:
        return {
            "dir": str(self.data_root),
            "seed-time": "0",
            "seed-ratio": "0",
            "file-allocation": "none",
            "auto-file-renaming": "false",
        }

    def set_file_playlist_enabled(
        self, gid: str, file_index: int, enabled: bool, *, force: bool = False
    ) -> Torrent:
        self._require_ready()
        # Sync first so a file that appeared after magnet metadata is known to
        # the API and the DB keeps the current aria2 selection/progress.
        self.sync_torrent(gid)
        row = self.db.torrent_file(gid, file_index)
        if row is None:
            raise TorrentClientError(f"unknown torrent file {gid}/{file_index}")
        metadata_file = is_metadata_path(row["path"])
        is_allowed = Path(row["path"]).suffix.lower() in self.allowed_extensions
        if enabled and metadata_file:
            raise TorrentClientError("magnet metadata is still resolving; wait for the torrent files")
        if enabled and not is_allowed and not force:
            raise TorrentClientError(
                "this file type is not allowed in the radio playlist; use the explicit "
                "media override only when you have verified the file contains audio"
            )
        self.db.set_torrent_file_enabled(
            gid, file_index, enabled, media_override=force and not is_allowed
        )
        # Once the admin has made a playlist choice, ask aria2 to download
        # only the chosen files. With no enabled files we leave aria2's
        # existing selection alone so metadata discovery still works.
        enabled_indexes = [
            int(row["file_index"])
            for row in self.db.torrent_files(gid)
            if bool(row["playlist_enabled"])
        ]
        if enabled_indexes:
            try:
                self.rpc.call(
                    "aria2.changeOption",
                    [gid, {"select-file": ",".join(str(index) for index in enabled_indexes)}],
                )
            except TorrentClientError as exc:
                # aria2 refuses option changes for completed/error jobs. The
                # playlist selection is still valid when the file is already
                # present on disk, so retain the DB selection in that case.
                torrent = self._torrent_from_db(gid)
                if torrent.status not in {"complete", "error", "paused"}:
                    raise
                log.warning("aria2 could not change file selection for %s: %s", gid, exc)
        return self._torrent_from_db(gid)

    def action(self, gid: str, action: str) -> Torrent | None:
        self._require_ready()
        action = action.lower().strip()
        method = {"pause": "aria2.pause", "resume": "aria2.unpause"}.get(action)
        if method is None:
            raise TorrentClientError(f"unknown torrent action {action!r}")
        self.rpc.call(method, [gid])
        return self.sync_torrent(gid)

    def remove(self, gid: str) -> list[tuple[str, str]]:
        self._require_ready()
        row = self.db.torrent(gid)
        with contextlib.suppress(TorrentClientError):
            self.rpc.call("aria2.forceRemove", [gid])
        removed = self.db.remove_torrent(gid)
        if row and row["source"]:
            source = Path(row["source"])
            try:
                source.resolve().relative_to(self.uploads_path.resolve())
            except ValueError:
                pass
            else:
                with contextlib.suppress(OSError):
                    source.unlink()
        return removed

    # ------------------------------------------------------------- file access
    @staticmethod
    def source_ref(gid: str, file_index: int) -> str:
        return f"{gid}:{int(file_index)}"

    @staticmethod
    def split_source_ref(source_ref: str) -> tuple[str, int]:
        try:
            gid, index = source_ref.rsplit(":", 1)
            return gid, int(index)
        except (ValueError, TypeError) as exc:
            raise ProviderFetchError(f"malformed torrent source_ref: {source_ref!r}") from exc

    def resolve_file(self, source_ref: str) -> Path:
        gid, file_index = self.split_source_ref(source_ref)
        # Completion can change while a track is waiting in the playlist.
        # Refresh this torrent just before playback so a file that finished in
        # the meantime becomes playable without a separate dashboard refresh.
        with contextlib.suppress(TorrentClientError):
            self.sync_torrent(gid)
        row = self.db.torrent_file(gid, file_index)
        if row is None:
            raise ProviderFetchError(f"unknown torrent file: {source_ref}")
        if not bool(row["is_complete"]):
            raise ProviderFetchError(
                f"torrent file is still downloading ({row['completed_length']}/{row['length']} bytes)"
            )
        path = Path(row["path"])
        if not path.is_absolute():
            path = self.data_root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(self.data_root.resolve())
        except ValueError as exc:
            raise ProviderFetchError("torrent file resolves outside torrent data directory") from exc
        if not resolved.is_file():
            raise ProviderFetchError(f"torrent file is missing: {resolved}")
        return resolved

    def ensure_cached(self, source_ref: str, target_path: Path) -> Path:
        source = self.resolve_file(source_ref)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            return target_path
        try:
            os.link(source, target_path)
        except OSError:
            try:
                shutil.copy2(source, target_path)
            except OSError as exc:
                raise ProviderFetchError(f"could not cache torrent file: {exc}") from exc
        return target_path
