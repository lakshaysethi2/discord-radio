"""Tests for the shared media-type classifier module.

Lock the exact accepted extensions/mimes down so accidental removals from
the frozenset trigger a test failure.
"""

from __future__ import annotations

from file_provider import media_types


class TestExtensionSets:
    def test_audio_exts_include_common_formats(self) -> None:
        for ext in (
            ".mp3",
            ".m4a",
            ".opus",
            ".ogg",
            ".flac",
            ".wav",
            ".aac",
            ".mka",
            ".ape",
            ".alac",
        ):
            assert ext in media_types.AUDIO_EXTS

    def test_video_exts_include_common_formats(self) -> None:
        for ext in (
            ".mp4",
            ".mkv",
            ".mov",
            ".avi",
            ".webm",
            ".m4v",
            ".m2ts",
            ".mts",
            ".vob",
            ".ogv",
        ):
            assert ext in media_types.VIDEO_EXTS

    def test_audio_and_video_are_disjoint(self) -> None:
        assert not (media_types.AUDIO_EXTS & media_types.VIDEO_EXTS)

    def test_playable_is_union(self) -> None:
        assert media_types.PLAYABLE_EXTS == media_types.AUDIO_EXTS | media_types.VIDEO_EXTS


class TestArchiveFormats:
    def test_audio_formats_include_mp3(self) -> None:
        for fmt in ("VBR MP3", "MP3", "FLAC", "WAVE"):
            assert fmt in media_types.AUDIO_ARCHIVE_FORMATS

    def test_video_formats_include_mp4(self) -> None:
        for fmt in ("MPEG4", "h.264", "Matroska", "QuickTime", "AVI"):
            assert fmt in media_types.VIDEO_ARCHIVE_FORMATS

    def test_playable_archive_is_union(self) -> None:
        assert (
            media_types.PLAYABLE_ARCHIVE_FORMATS
            == media_types.AUDIO_ARCHIVE_FORMATS | media_types.VIDEO_ARCHIVE_FORMATS
        )


class TestClassifiers:
    def test_is_video_ext(self) -> None:
        assert media_types.is_video_ext(".mp4") is True
        assert media_types.is_video_ext(".MKV") is True  # case-insensitive
        assert media_types.is_video_ext(".mp3") is False
        assert media_types.is_video_ext("mp4") is False  # no leading dot

    def test_is_playable_ext(self) -> None:
        assert media_types.is_playable_ext(".mp3") is True
        assert media_types.is_playable_ext(".MP4") is True
        assert media_types.is_playable_ext(".txt") is False
        assert media_types.is_playable_ext("") is False

    def test_is_video_mime(self) -> None:
        assert media_types.is_video_mime("video/mp4") is True
        assert media_types.is_video_mime("VIDEO/webm") is True
        assert media_types.is_video_mime("audio/mpeg") is False
        assert media_types.is_video_mime("") is False
        assert media_types.is_video_mime(None) is False  # type: ignore[arg-type]

    def test_is_playable_mime(self) -> None:
        assert media_types.is_playable_mime("audio/mpeg") is True
        assert media_types.is_playable_mime("video/mp4") is True
        assert media_types.is_playable_mime("text/plain") is False
        assert media_types.is_playable_mime("") is False
