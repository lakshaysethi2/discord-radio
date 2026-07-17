"""Tests for the archive.org (Internet Archive) provider.

Uses respx to mock httpx so no real network is hit.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from file_provider.providers.archive import (
    METADATA_URL,
    ArchiveOrgProvider,
)
from file_provider.providers.base import ProviderFetchError

ITEM = "Hawkins_Lectures_transcoded_actual_files"


# --- realistic subset of what archive.org returns for the Hawkins item ------
SAMPLE_METADATA = {
    "files": [
        {
            "name": "BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3",
            "source": "original",
            "format": "VBR MP3",
            "size": "11562734",
            "length": "5779.12",
        },
        {
            "name": "BTO Radio Interviews/#01 - 11_08_01 - #4193.afpk",
            "source": "derivative",
            "format": "Columbia Peaks",
            "size": "953816",
        },
        {
            "name": "BTO Radio Interviews/#01 - 11_08_01 - #4193.png",
            "source": "derivative",
            "format": "PNG",
            "size": "34700",
        },
        {
            "name": "BTO Radio Interviews/#02 - 02_21_02 - #70AB.mp3",
            "source": "original",
            "format": "VBR MP3",
            "size": "5690199",
            "length": "2842.85",
        },
        # Non-standard duration format — HH:MM:SS.
        {
            "name": "Lectures/lecture-01.mp3",
            "source": "original",
            "format": "VBR MP3",
            "size": "10000000",
            "length": "1:02:03",
        },
        # Something with no explicit format but valid audio extension.
        {
            "name": "Extras/bonus.opus",
            "source": "original",
            "size": "500000",
        },
        # A video file — must be excluded.
        {
            "name": "Video/lecture.mp4",
            "source": "original",
            "format": "MPEG4",
            "size": "999999",
        },
    ],
}


# ================================================================== fixtures
@pytest.fixture
def provider() -> ArchiveOrgProvider:
    return ArchiveOrgProvider(item_ids=[ITEM])


# ==================================================================== config
class TestConfig:
    def test_is_configured_requires_ids(self) -> None:
        assert ArchiveOrgProvider(item_ids=[]).is_configured() is False
        assert ArchiveOrgProvider(item_ids=[""]).is_configured() is False
        assert ArchiveOrgProvider(item_ids=["x"]).is_configured() is True

    def test_ids_are_trimmed(self) -> None:
        p = ArchiveOrgProvider(item_ids=[" a ", "", " b"])
        assert p.item_ids == ["a", "b"]


# ==================================================================== scan
class TestScan:
    @respx.mock
    def test_scan_returns_originals_including_video(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(200, json=SAMPLE_METADATA)
        )
        tracks = provider.list_tracks()
        names = {t.source_ref.split("::", 1)[1] for t in tracks}
        # 5 originals: two mp3s, one nested mp3, one opus, one mp4 (video container
        # with audio track — FFmpeg strips the video). Derivatives excluded.
        assert names == {
            "BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3",
            "BTO Radio Interviews/#02 - 02_21_02 - #70AB.mp3",
            "Lectures/lecture-01.mp3",
            "Extras/bonus.opus",
            "Video/lecture.mp4",
        }

    @respx.mock
    def test_video_files_flagged_has_video(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(200, json=SAMPLE_METADATA)
        )
        tracks = {t.source_ref.split("::", 1)[1]: t for t in provider.list_tracks()}
        assert tracks["Video/lecture.mp4"].has_video is True
        assert tracks["BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3"].has_video is False

    @respx.mock
    def test_duration_parsed(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(200, json=SAMPLE_METADATA)
        )
        tracks = {t.source_ref.split("::", 1)[1]: t for t in provider.list_tracks()}
        # Float-seconds format.
        assert tracks["BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3"].duration_seconds == 5779
        # HH:MM:SS format → 1*3600 + 2*60 + 3 = 3723.
        assert tracks["Lectures/lecture-01.mp3"].duration_seconds == 3723
        # Missing length → 0.
        assert tracks["Extras/bonus.opus"].duration_seconds == 0

    @respx.mock
    def test_size_parsed(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(200, json=SAMPLE_METADATA)
        )
        tracks = {t.source_ref.split("::", 1)[1]: t for t in provider.list_tracks()}
        assert tracks["BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3"].size_bytes == 11562734

    @respx.mock
    def test_source_ref_packs_item_id_and_path(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(200, json=SAMPLE_METADATA)
        )
        tracks = provider.list_tracks()
        for t in tracks:
            assert "::" in t.source_ref
            item_id, _path = t.source_ref.split("::", 1)
            assert item_id == ITEM

    @respx.mock
    def test_multiple_items_scanned(self) -> None:
        provider = ArchiveOrgProvider(item_ids=["one", "two"])
        respx.get(METADATA_URL.format(item_id="one")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"name": "a.mp3", "source": "original", "format": "VBR MP3", "size": "1"},
                    ]
                },
            )
        )
        respx.get(METADATA_URL.format(item_id="two")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"name": "b.mp3", "source": "original", "format": "VBR MP3", "size": "1"},
                    ]
                },
            )
        )
        tracks = provider.list_tracks()
        assert len(tracks) == 2
        # Sorted stably by source_ref (item id prefix).
        assert tracks[0].source_ref.startswith("one::")
        assert tracks[1].source_ref.startswith("two::")

    @respx.mock
    def test_scan_survives_one_item_failure(self) -> None:
        provider = ArchiveOrgProvider(item_ids=["ok", "broken"])
        respx.get(METADATA_URL.format(item_id="ok")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"name": "a.mp3", "source": "original", "format": "VBR MP3", "size": "1"},
                    ]
                },
            )
        )
        respx.get(METADATA_URL.format(item_id="broken")).mock(
            return_value=httpx.Response(500, text="server error")
        )
        tracks = provider.list_tracks()
        # The 'ok' item's track is still there.
        assert len(tracks) == 1
        assert tracks[0].source_ref.startswith("ok::")

    def test_scan_without_config_returns_empty(self) -> None:
        assert ArchiveOrgProvider(item_ids=[]).list_tracks() == []

    @respx.mock
    def test_title_leaf_default(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "name": "SomeFolder/A meaningful filename.mp3",
                            "source": "original",
                            "format": "VBR MP3",
                            "size": "1",
                        },
                    ]
                },
            )
        )
        tracks = provider.list_tracks()
        assert "A meaningful filename" in tracks[0].title

    @respx.mock
    def test_title_generic_gets_parent(self, provider: ArchiveOrgProvider) -> None:
        respx.get(METADATA_URL.format(item_id=ITEM)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "name": "Lecture 5/track01.mp3",
                            "source": "original",
                            "format": "VBR MP3",
                            "size": "1",
                        },
                    ]
                },
            )
        )
        tracks = provider.list_tracks()
        # Generic-looking leaf gets folder context.
        assert "Lecture 5" in tracks[0].title


# ==================================================================== fetch
class TestFetch:
    @respx.mock
    def test_downloads_to_target(self, tmp_path: Path, provider: ArchiveOrgProvider) -> None:
        payload = b"MP3 BYTES " * 200
        respx.get(
            "https://archive.org/download/"
            "Hawkins_Lectures_transcoded_actual_files/"
            "BTO%20Radio%20Interviews/%2301%20-%2011_08_01%20-%20%234193.mp3"
        ).mock(return_value=httpx.Response(200, content=payload))

        target = tmp_path / "x.mp3"
        source_ref = (
            "Hawkins_Lectures_transcoded_actual_files::"
            "BTO Radio Interviews/#01 - 11_08_01 - #4193.mp3"
        )
        got = provider.ensure_cached(source_ref, target)
        assert got == target
        assert target.read_bytes() == payload

    @respx.mock
    def test_idempotent_when_cached(self, tmp_path: Path, provider: ArchiveOrgProvider) -> None:
        target = tmp_path / "x.mp3"
        target.write_bytes(b"already here")
        # No HTTP mock registered — respx would fail if called.
        got = provider.ensure_cached("item::path.mp3", target)
        assert got == target
        assert target.read_bytes() == b"already here"

    @respx.mock
    def test_http_error_raises_and_cleans_up(
        self, tmp_path: Path, provider: ArchiveOrgProvider
    ) -> None:
        respx.get("https://archive.org/download/item/broken.mp3").mock(
            return_value=httpx.Response(404)
        )
        target = tmp_path / "x.mp3"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("item::broken.mp3", target)
        # Partial file must not linger.
        assert not target.exists()
        assert not target.with_suffix(target.suffix + ".part").exists()

    @respx.mock
    def test_network_error_wrapped(self, tmp_path: Path, provider: ArchiveOrgProvider) -> None:
        respx.get("https://archive.org/download/item/net.mp3").mock(
            side_effect=httpx.ConnectError("boom")
        )
        target = tmp_path / "x.mp3"
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("item::net.mp3", target)
        assert not target.exists()

    def test_malformed_source_ref_rejected(
        self, tmp_path: Path, provider: ArchiveOrgProvider
    ) -> None:
        with pytest.raises(ProviderFetchError):
            provider.ensure_cached("no-separator-here", tmp_path / "x.mp3")

    @respx.mock
    def test_url_escapes_hashes_and_spaces(
        self, tmp_path: Path, provider: ArchiveOrgProvider
    ) -> None:
        """The tricky filenames on archive.org have '#' and spaces — they must
        be percent-encoded in the URL. This test locks that behaviour in."""
        route = respx.get(
            "https://archive.org/download/item/sub%20folder/track%20%231%20%23name.mp3"
        ).mock(return_value=httpx.Response(200, content=b"x"))
        target = tmp_path / "x.mp3"
        provider.ensure_cached("item::sub folder/track #1 #name.mp3", target)
        assert route.called
