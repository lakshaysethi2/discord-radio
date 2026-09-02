"""Tests for the WebDAV (rclone serve webdav) provider.

Uses respx to mock httpx so no real network is hit.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from file_provider.providers.base import ProviderFetchError
from file_provider.providers.webdav import WebDavProvider

BASE = "http://webdav.test:8081"

# --- realistic-ish multistatus bodies ----------------------------------------
_ROOT_MS = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/Lectures2002-2011/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/VolumeSeries/volume-i-power-vs-force.mp4</D:href>
    <D:propstat><D:prop>
      <D:getcontentlength>123456</D:getcontentlength>
      <D:getcontenttype>video/mp4</D:getcontenttype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/VolumeSeries/notes.pdf</D:href>
    <D:propstat><D:prop>
      <D:getcontentlength>999</D:getcontentlength>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>"""

_LECTURES_MS = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/Lectures2002-2011/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/Lectures2002-2011/2002/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>"""

_Y2002_MS = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/Lectures2002-2011/2002/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/Lectures2002-2011/2002/apr-2002.mp4</D:href>
    <D:propstat><D:prop>
      <D:getcontentlength>456789</D:getcontentlength>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>"""


@pytest.fixture
def provider() -> WebDavProvider:
    return WebDavProvider(url=BASE)


# ==================================================================== config
class TestConfig:
    def test_is_configured_requires_url(self) -> None:
        assert WebDavProvider(url="").is_configured() is False
        assert WebDavProvider(url="http://x").is_configured() is True

    def test_path_normalized(self) -> None:
        assert WebDavProvider(url=BASE, path="/").path == "/"
        assert WebDavProvider(url=BASE, path="").path == "/"
        assert WebDavProvider(url=BASE, path="mother-of-all-torrents").path == "/mother-of-all-torrents"


# ==================================================================== scan
class TestScan:
    @respx.mock
    def test_scan_recurses_and_filters_playables(self, provider: WebDavProvider) -> None:
        respx.request("PROPFIND", f"{BASE}/").mock(return_value=httpx.Response(200, content=_ROOT_MS))
        respx.request("PROPFIND", f"{BASE}/Lectures2002-2011").mock(
            return_value=httpx.Response(200, content=_LECTURES_MS)
        )
        respx.request("PROPFIND", f"{BASE}/Lectures2002-2011/2002").mock(
            return_value=httpx.Response(200, content=_Y2002_MS)
        )
        tracks = provider.list_tracks()
        refs = {t.source_ref for t in tracks}
        assert refs == {
            "VolumeSeries/volume-i-power-vs-force.mp4",
            "Lectures2002-2011/2002/apr-2002.mp4",
        }
        by_ref = {t.source_ref: t for t in tracks}
        assert by_ref["VolumeSeries/volume-i-power-vs-force.mp4"].has_video is True
        assert by_ref["VolumeSeries/volume-i-power-vs-force.mp4"].size_bytes == 123456
        assert by_ref["Lectures2002-2011/2002/apr-2002.mp4"].has_video is True

    @respx.mock
    def test_scan_without_config_returns_empty(self) -> None:
        assert WebDavProvider(url="").list_tracks() == []

    @respx.mock
    def test_scan_fails_closed_on_broken_subdir(self, provider: WebDavProvider) -> None:
        respx.request("PROPFIND", f"{BASE}/").mock(return_value=httpx.Response(200, content=_ROOT_MS))
        respx.request("PROPFIND", f"{BASE}/Lectures2002-2011/").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(ProviderFetchError, match="PROPFIND"):
            provider.list_tracks()

    @respx.mock
    def test_scan_absolute_hrefs_and_percent_encoding(self, provider: WebDavProvider) -> None:
        ms = """<?xml version="1.0"?>
        <D:multistatus xmlns:D="DAV:">
          <D:response>
            <D:href>http://webdav.test:8081/Some%20Folder/track%20%231.mp3</D:href>
            <D:propstat><D:prop><D:getcontentlength>11</D:getcontentlength>
            </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
          </D:response>
        </D:multistatus>"""
        respx.request("PROPFIND", f"{BASE}/").mock(return_value=httpx.Response(200, content=ms))
        tracks = provider.list_tracks()
        assert [t.source_ref for t in tracks] == ["Some Folder/track #1.mp3"]

    @respx.mock
    def test_scan_scoped_to_configured_path(self) -> None:
        provider = WebDavProvider(url=BASE, path="/Lectures2002-2011")
        respx.request("PROPFIND", f"{BASE}/Lectures2002-2011").mock(
            return_value=httpx.Response(200, content=_LECTURES_MS)
        )
        respx.request("PROPFIND", f"{BASE}/Lectures2002-2011/2002").mock(
            return_value=httpx.Response(200, content=_Y2002_MS)
        )
        tracks = provider.list_tracks()
        assert {t.source_ref for t in tracks} == {"2002/apr-2002.mp4"}


# ==================================================================== fetch
class TestFetch:
    @respx.mock
    def test_downloads_to_target(self, tmp_path: Path, provider: WebDavProvider) -> None:
        payload = b"MP4 BYTES " * 200
        respx.get(f"{BASE}/VolumeSeries/volume-i-power-vs-force.mp4").mock(
            return_value=httpx.Response(200, content=payload)
        )
        target = tmp_path / "x.mp4"
        got = provider.ensure_cached("VolumeSeries/volume-i-power-vs-force.mp4", target)
        assert got == target
        assert target.read_bytes() == payload

    @respx.mock
    def test_url_escapes_hashes_and_spaces(self, tmp_path: Path, provider: WebDavProvider) -> None:
        route = respx.get(f"{BASE}/Some%20Folder/track%20%231.mp3").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        provider.ensure_cached("Some Folder/track #1.mp3", tmp_path / "x.mp3")
        assert route.called

    @respx.mock
    def test_idempotent_when_cached(self, tmp_path: Path, provider: WebDavProvider) -> None:
        target = tmp_path / "x.mp4"
        target.write_bytes(b"already here")
        got = provider.ensure_cached("a/b.mp4", target)
        assert got == target
        assert target.read_bytes() == b"already here"

    @respx.mock
    def test_http_error_raises_and_cleans_up(
        self, tmp_path: Path, provider: WebDavProvider
    ) -> None:
        respx.get(f"{BASE}/broken.mp4").mock(return_value=httpx.Response(404))
        target = tmp_path / "x.mp4"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("broken.mp4", target)
        assert not target.exists()
        assert not target.with_suffix(target.suffix + ".part").exists()

    @respx.mock
    def test_network_error_wrapped(self, tmp_path: Path, provider: WebDavProvider) -> None:
        respx.get(f"{BASE}/net.mp4").mock(side_effect=httpx.ConnectError("boom"))
        target = tmp_path / "x.mp4"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("net.mp4", target)
        assert not target.exists()

    @respx.mock
    def test_scoped_fetch_keeps_base_prefix(self, tmp_path: Path) -> None:
        provider = WebDavProvider(url=BASE, path="/Lectures2002-2011")
        respx.get(f"{BASE}/Lectures2002-2011/2002/apr-2002.mp4").mock(
            return_value=httpx.Response(200, content=b"y")
        )
        provider.ensure_cached("2002/apr-2002.mp4", tmp_path / "x.mp4")
