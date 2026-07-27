"""Tests for the HTTP media provider (rclone/nginx directory listings).

Uses respx to mock httpx so no real network is hit.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from file_provider.providers.base import ProviderFetchError
from file_provider.providers.http_media import HttpMediaProvider, _is_playable_link, _is_dir_link

# ---- rclone-style directory listing HTML (subset of what rclone serve http emits) ----
DIR_INDEX_HTML = """<!DOCTYPE html>
<html><body>
<h1>Index of /media</h1>
<a href="lecture-01.mp3">lecture-01.mp3</a>
<a href="lecture-02.mp3">lecture-02.mp3</a>
<a href="BTO%20Radio/">BTO Radio/</a>
<a href="video_talk.mp4">video_talk.mp4</a>
<a href="notes.txt">notes.txt</a>
<a href="cover.jpg">cover.jpg</a>
</body></html>"""

SUBDIR_INDEX_HTML = """<!DOCTYPE html>
<html><body>
<h1>Index of /media/BTO Radio/</h1>
<a href="../">../</a>
<a href="part01.mp3">part01.mp3</a>
<a href="part02.mp3">part02.mp3</a>
<a href="show_notes.pdf">show_notes.pdf</a>
</body></table>
</body></html>"""

EMPTY_DIR_HTML = """<!DOCTYPE html>
<html><body>
<h1>Index of /media/empty/</h1>
</body></html>"""


# ==================================================================== helpers
class TestLinkParsing:
    def test_playable_audio(self) -> None:
        assert _is_playable_link("track.mp3") is True
        assert _is_playable_link("track.flac") is True
        assert _is_playable_link("track.opus") is True
        assert _is_playable_link("track.m4a") is True
        assert _is_playable_link("track.ogg") is True

    def test_playable_video(self) -> None:
        assert _is_playable_link("video.mp4") is True
        assert _is_playable_link("video.mkv") is True
        assert _is_playable_link("video.webm") is True

    def test_skips_directories(self) -> None:
        assert _is_playable_link("subdir/") is False
        assert _is_playable_link("./") is False
        assert _is_playable_link("../") is False
        assert _is_playable_link("/") is False

    def test_skips_non_media(self) -> None:
        assert _is_playable_link("notes.txt") is False
        assert _is_playable_link("cover.jpg") is False
        assert _is_playable_link("doc.pdf") is False
        assert _is_playable_link("page.html") is False

    def test_dir_link_detection(self) -> None:
        assert _is_dir_link("BTO Radio/") is True
        assert _is_dir_link("subdir/") is True
        # Parent-dir links are NOT treated as dirs (prevents infinite recursion)
        assert _is_dir_link("../") is False
        assert _is_dir_link("track.mp3") is False
        assert _is_dir_link("notes.txt") is False


# ================================================================== fixtures
@pytest.fixture
def provider() -> HttpMediaProvider:
    return HttpMediaProvider(base_url="http://media.example.invalid/library")


# ==================================================================== config
class TestConfig:
    def test_is_configured_requires_base_url(self) -> None:
        assert HttpMediaProvider(base_url="").is_configured() is False
        assert HttpMediaProvider(base_url="http://example.com").is_configured() is True

    def test_password_not_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Password values must never appear in log output."""
        import logging

        caplog.set_level(logging.DEBUG)
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid",
            username="admin",
            password="super-secret-password-12345",
        )
        # Trigger a log-worthy event: scan with connection error
        with respx.mock:
            respx.get("http://media.example.invalid/").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            provider.list_tracks()
        log_text = caplog.text
        assert "super-secret-password-12345" not in log_text
        assert "admin" not in log_text  # username also shouldn't appear


# ==================================================================== scan
class TestScan:
    @respx.mock
    def test_scan_flat_directory(self, provider: HttpMediaProvider) -> None:
        route = respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert names == {"lecture-01.mp3", "lecture-02.mp3", "video_talk.mp4"}
        assert route.called

    @respx.mock
    def test_scan_recursive(self, provider: HttpMediaProvider) -> None:
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        respx.get("http://media.example.invalid/library/BTO%20Radio/").mock(
            return_value=httpx.Response(200, text=SUBDIR_INDEX_HTML)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert names == {
            "lecture-01.mp3",
            "lecture-02.mp3",
            "video_talk.mp4",
            "BTO Radio/part01.mp3",
            "BTO Radio/part02.mp3",
        }
        assert len(tracks) == 5

    @respx.mock
    def test_scan_empty_directory(self, provider: HttpMediaProvider) -> None:
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=EMPTY_DIR_HTML)
        )
        tracks = provider.list_tracks()
        assert tracks == []

    @respx.mock
    def test_video_track_flagged(self, provider: HttpMediaProvider) -> None:
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        tracks = {t.source_ref: t for t in provider.list_tracks()}
        assert tracks["video_talk.mp4"].has_video is True
        assert tracks["lecture-01.mp3"].has_video is False

    @respx.mock
    def test_scan_survives_http_error(self, provider: HttpMediaProvider) -> None:
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(500, text="server error")
        )
        tracks = provider.list_tracks()
        assert tracks == []

    def test_scan_without_config_returns_empty(self) -> None:
        assert HttpMediaProvider(base_url="").list_tracks() == []

    @respx.mock
    def test_scan_survives_subdir_failure(self, provider: HttpMediaProvider) -> None:
        """If one subdirectory fails to load, the rest of the scan continues."""
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        respx.get("http://media.example.invalid/library/BTO%20Radio/").mock(
            return_value=httpx.Response(503, text="unavailable")
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        # Files from the root directory are still returned.
        assert "lecture-01.mp3" in names

    @respx.mock
    def test_track_stable_order(self, provider: HttpMediaProvider) -> None:
        """Tracks are sorted by source_ref for deterministic ordering."""
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        tracks = provider.list_tracks()
        refs = [t.source_ref for t in tracks]
        assert refs == sorted(refs)

    @respx.mock
    def test_non_recursive_flat_only(self) -> None:
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library", recursive=False
        )
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert "BTO Radio/part01.mp3" not in names  # no recursion
        assert "lecture-01.mp3" in names

    # ---- Scan-boundary safety tests ----
    # These simulate the real-world scenario where rclone serve http
    # shows directory listings with absolute links ("/Inbox/"),
    # parent-dir links ("../"), or links to sibling/outside trees.

    @respx.mock
    def test_absolute_root_link_skipped(self) -> None:
        """An href like "/Inbox/" points outside the base path and must
        be skipped to prevent escape-and-re-enter cycles."""
        HTML_WITH_ABSOLUTE = """<html><body>
<a href="track.mp3">track.mp3</a>
<a href="/Other/">Other</a>
<a href="/Inbox/">Inbox</a>
</body></html>"""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library/radio"
        )
        respx.get("http://media.example.invalid/library/radio/").mock(
            return_value=httpx.Response(200, text=HTML_WITH_ABSOLUTE)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert names == {"track.mp3"}
        assert len(tracks) == 1

    @respx.mock
    def test_parent_dir_link_skipped(self) -> None:
        """A "../" href back to the parent directory must be skipped."""
        HTML_WITH_PARENT = """<html><body>
<a href="../">../</a>
<a href="sub/">sub/</a>
<a href="track.mp3">track.mp3</a>
</body></html>"""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library"
        )
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=HTML_WITH_PARENT)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert "track.mp3" in names
        assert "../track.mp3" not in names

    @respx.mock
    def test_visited_set_prevents_cycle(self) -> None:
        """If two directory entries point to the same normalized URL,
        the visited set prevents re-entering it."""
        HTML = """<html><body>
<a href="samedir/">samedir</a>
<a href="./">self</a>
<a href="track.mp3">track.mp3</a>
</body></html>"""
        SUB_HTML = """<html><body>
<a href="../">../</a>
<a href="sub_track.mp3">sub_track.mp3</a>
</body></html>"""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library"
        )
        # Root dir
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=HTML)
        )
        # Subdir "samedir/" (resolves to same as root via ./ but visited set
        # catches the canonicalized form)
        respx.get("http://media.example.invalid/library/samedir/").mock(
            return_value=httpx.Response(200, text=SUB_HTML)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        # Should have files from both root and subdir, but no duplicates.
        assert "track.mp3" in names
        assert "samedir/sub_track.mp3" in names
        # The visited set prevents re-entering root via ../ from subdir:
        # "../" is always skipped, so no infinite loop.

    @respx.mock
    def test_sibling_dir_outside_base_skipped(self) -> None:
        """A link to a sibling directory whose path does NOT start with
        base_path must be ignored (prevents scanning outside the configured
        library)."""
        HTML_WITH_SIBLING = """<html><body>
<a href="track.mp3">track.mp3</a>
<a href="other_lib/">other_lib</a>
<a href="/library/other_lib/">other_lib (abs)</a>
</body></html>"""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library/radio"
        )
        respx.get("http://media.example.invalid/library/radio/").mock(
            return_value=httpx.Response(200, text=HTML_WITH_SIBLING)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        # "other_lib/" resolves to /library/radio/other_lib/ → under base_path,
        # so it IS scanned. But "/library/other_lib/" resolves to
        # /library/other_lib/ → outside base_path /library/radio/, skipped.
        assert "track.mp3" in names

    @respx.mock
    def test_visit_same_dir_twice_no_duplicates(self) -> None:
        """Even if the HTML contains both '.' and the dir name, the visited
        set prevents double-scanning."""
        HTML = """<html><body>
<a href="./">.</a>
<a href="sub/">sub/</a>
<a href="track.mp3">track.mp3</a>
</body></html>"""
        SUB_HTML = """<html><body>
<a href="sub_track.mp3">sub_track.mp3</a>
</body></html>"""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library"
        )
        respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=HTML)
        )
        respx.get("http://media.example.invalid/library/sub/").mock(
            return_value=httpx.Response(200, text=SUB_HTML)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref for t in tracks}
        assert "track.mp3" in names
        assert "sub/sub_track.mp3" in names
        # Each file should appear exactly once.
        assert len(names) == 2

    @respx.mock
    def test_basic_auth_header_present(self) -> None:
        """When username/password are set, the Authorization header must be sent."""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library",
            username="testuser",
            password="testpass",
        )
        route = respx.get("http://media.example.invalid/library/").mock(
            return_value=httpx.Response(200, text=DIR_INDEX_HTML)
        )
        provider.list_tracks()
        assert route.called
        request = route.calls[0].request
        auth_header = request.headers.get("Authorization", "")
        assert auth_header.startswith("Basic ")
        # Verify it's valid base64-encoded credentials
        import base64

        decoded = base64.b64decode(auth_header.replace("Basic ", "", 1)).decode()
        assert decoded == "testuser:testpass"


# ==================================================================== fetch
class TestFetch:
    @respx.mock
    def test_downloads_to_target(self, tmp_path: Path) -> None:
        provider = HttpMediaProvider(base_url="http://media.example.invalid/library")
        payload = b"MP3 BYTES " * 200
        respx.get("http://media.example.invalid/library/lecture-01.mp3").mock(
            return_value=httpx.Response(200, content=payload)
        )
        target = tmp_path / "x.mp3"
        got = provider.ensure_cached("lecture-01.mp3", target)
        assert got == target
        assert target.read_bytes() == payload

    @respx.mock
    def test_download_follows_redirects(self, tmp_path: Path) -> None:
        """The provider must follow HTTP redirects (e.g., 302, 307)."""
        provider = HttpMediaProvider(base_url="http://media.example.invalid/library")
        payload = b"redirected content"
        redirect_url = "http://cdn.example.invalid/real-file.mp3"
        respx.get("http://media.example.invalid/library/lecture-01.mp3").mock(
            return_value=httpx.Response(302, headers={"Location": redirect_url})
        )
        respx.get(redirect_url).mock(
            return_value=httpx.Response(200, content=payload)
        )
        target = tmp_path / "x.mp3"
        got = provider.ensure_cached("lecture-01.mp3", target)
        assert got == target
        assert target.read_bytes() == payload

    @respx.mock
    def test_idempotent_when_cached(self, tmp_path: Path) -> None:
        provider = HttpMediaProvider(base_url="http://media.example.invalid/library")
        target = tmp_path / "x.mp3"
        target.write_bytes(b"already here")
        # No HTTP mock registered — would fail if called.
        got = provider.ensure_cached("any.mp3", target)
        assert got == target

    @respx.mock
    def test_http_error_raises_and_cleans_up(self, tmp_path: Path) -> None:
        provider = HttpMediaProvider(base_url="http://media.example.invalid/library")
        respx.get("http://media.example.invalid/library/broken.mp3").mock(
            return_value=httpx.Response(404)
        )
        target = tmp_path / "x.mp3"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("broken.mp3", target)
        assert not target.exists()
        assert not target.with_suffix(".part").exists()

    @respx.mock
    def test_network_error_wrapped(self, tmp_path: Path) -> None:
        provider = HttpMediaProvider(base_url="http://media.example.invalid/library")
        respx.get("http://media.example.invalid/library/net.mp3").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        target = tmp_path / "x.mp3"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("net.mp3", target)
        assert not target.exists()

    def test_not_configured_raises(self, tmp_path: Path) -> None:
        provider = HttpMediaProvider(base_url="")
        with pytest.raises(ProviderFetchError, match="not configured"):
            provider.ensure_cached("any.mp3", tmp_path / "x.mp3")

    @respx.mock
    def test_basic_auth_on_download(self, tmp_path: Path) -> None:
        """Download requests must include Basic auth when configured."""
        provider = HttpMediaProvider(
            base_url="http://media.example.invalid/library",
            username="downloader",
            password="dl-secret",
        )
        payload = b"authenticated content"
        route = respx.get("http://media.example.invalid/library/auth-file.mp3").mock(
            return_value=httpx.Response(200, content=payload)
        )
        target = tmp_path / "x.mp3"
        provider.ensure_cached("auth-file.mp3", target)
        request = route.calls[0].request
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Basic ")
        import base64

        decoded = base64.b64decode(auth.replace("Basic ", "", 1)).decode()
        assert decoded == "downloader:dl-secret"
        assert target.read_bytes() == payload
