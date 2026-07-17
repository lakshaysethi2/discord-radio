from __future__ import annotations

from pathlib import Path

import pytest

from file_provider.db import ProviderDB
from file_provider.providers.torrent import TorrentProvider
from file_provider.storage import StorageQuota
from file_provider.torrent_client import (
    TorrentClientError,
    TorrentManager,
    TorrentSecurityError,
    TorrentSizeLimitError,
    TorrentStorageLimitError,
    is_metadata_path,
    validate_rpc_url,
)


class FakeRpc:
    def __init__(self, root: Path, *, include_name: bool = True) -> None:
        self.root = root
        self.include_name = include_name
        self.calls: list[tuple[str, list | None]] = []
        self.files = [
            {
                "index": "1",
                "path": str(root / "album" / "song.mp3"),
                "length": "4",
                "completedLength": "4",
                "selected": "true",
            },
            {
                "index": "2",
                "path": str(root / "album" / "notes.txt"),
                "length": "5",
                "completedLength": "5",
                "selected": "true",
            },
        ]

    def call(self, method: str, params=None):
        self.calls.append((method, params))
        if method == "aria2.getVersion":
            return {"version": "fake"}
        if method == "aria2.addUri":
            return "gid-a"
        if method == "aria2.addTorrent":
            assert params[1] == []
            return "gid-upload"
        if method == "aria2.tellStatus":
            return {
                "gid": params[0],
                "status": "complete",
                "totalLength": "9",
                "completedLength": "9",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
                "errorCode": "0",
                "errorMessage": "",
                "infoHash": "abc123",
                "bittorrent": {"info": {"name": "Album" if self.include_name else ""}},
            }
        if method == "aria2.getFiles":
            return self.files
        if method == "aria2.changeOption":
            return "OK"
        if method == "aria2.forceRemove":
            return "OK"
        raise AssertionError(f"unexpected RPC method {method}")


def test_torrent_manager_indexes_files_and_enables_playlist(tmp_path: Path) -> None:
    root = tmp_path / "torrents"
    (root / "album").mkdir(parents=True)
    (root / "album" / "song.mp3").write_bytes(b"song")
    db = ProviderDB(tmp_path / "provider.db")
    rpc = FakeRpc(root)
    manager = TorrentManager(db, root, rpc=rpc)

    assert manager.start() is True
    torrent = manager.add_magnet("magnet:?xt=urn:btih:abc123")
    assert torrent.gid == "gid-a"
    assert torrent.files[0].is_complete is True
    assert torrent.files[0].playable is True
    assert torrent.files[1].playable is False

    manager.set_file_playlist_enabled("gid-a", 1, True)
    row = db.torrent_file("gid-a", 1)
    assert row["playlist_enabled"] == 1
    assert any(method == "aria2.changeOption" for method, _ in rpc.calls)

    # An administrator can explicitly override the extension guard after
    # verifying an otherwise unknown file is media.
    manager.set_file_playlist_enabled("gid-a", 2, True, force=True)
    override = db.torrent_file("gid-a", 2)
    assert override["media_override"] == 1

    provider = TorrentProvider(manager)
    tracks = provider.list_tracks()
    assert [track.title for track in tracks] == ["Album — song.mp3", "Album — notes.txt"]

    target = tmp_path / "cache" / "song.audio"
    assert manager.ensure_cached("gid-a:1", target) == target
    assert target.read_bytes() == b"song"
    manager.stop()
    db.close()


def test_metadata_pseudo_file_is_not_media() -> None:
    assert is_metadata_path("[METADATA]example") is True
    assert is_metadata_path("/data/torrents/album/song.mp3") is False


def test_rpc_defaults_to_loopback_only() -> None:
    validate_rpc_url("http://127.0.0.1:6800/jsonrpc")
    validate_rpc_url("http://[::1]:6800/jsonrpc")
    with pytest.raises(TorrentSecurityError):
        validate_rpc_url("http://aria2.internal:6800/jsonrpc")
    validate_rpc_url("http://aria2.internal:6800/jsonrpc", allow_remote=True)


def test_torrent_name_falls_back_to_relative_download_directory(tmp_path: Path) -> None:
    root = tmp_path / "torrents"
    (root / "album").mkdir(parents=True)
    (root / "album" / "song.mp3").write_bytes(b"song")
    db = ProviderDB(tmp_path / "provider.db")
    manager = TorrentManager(db, root, rpc=FakeRpc(root, include_name=False))
    manager.start()
    assert manager.add_magnet("magnet:?xt=urn:btih:abc123").name == "album"
    db.close()


def test_oversized_torrent_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "torrents"
    root.mkdir()
    db = ProviderDB(tmp_path / "provider.db")
    manager = TorrentManager(db, root, rpc=FakeRpc(root), max_size_bytes=8)
    manager.start()
    with pytest.raises(TorrentSizeLimitError):
        manager.add_magnet("magnet:?xt=urn:btih:abc123")
    assert db.list_torrents() == []
    db.close()


def test_add_torrent_uses_aria2_signature(tmp_path: Path) -> None:
    db = ProviderDB(tmp_path / "provider.db")
    manager = TorrentManager(db, tmp_path / "torrents", rpc=FakeRpc(tmp_path))
    manager.start()
    assert manager.add_torrent_file(b"torrent-bytes").gid == "gid-upload"
    db.close()


def test_global_storage_limit_pauses_new_download(tmp_path: Path) -> None:
    db = ProviderDB(tmp_path / "provider.db")
    root = tmp_path / "torrents"
    quota = StorageQuota(8, [root])
    manager = TorrentManager(db, root, rpc=FakeRpc(root), quota=quota)
    manager.start()
    with pytest.raises(TorrentStorageLimitError):
        manager.add_magnet("magnet:?xt=urn:btih:abc123")
    assert db.torrent("gid-a")["status"] == "paused"
    db.close()


def test_torrent_upload_limit_is_enforced_before_rpc(tmp_path: Path) -> None:
    db = ProviderDB(tmp_path / "provider.db")
    manager = TorrentManager(db, tmp_path / "torrents", rpc=FakeRpc(tmp_path), max_upload_bytes=3)
    with pytest.raises(TorrentClientError, match="upload exceeds"):
        manager.add_torrent_file(b"1234")
    db.close()
