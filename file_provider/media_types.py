"""Shared media-type constants across all providers.

Extracted so every backend (local, archive, telegram) uses the exact same
audio + video file extension / mime-type set. Adding a new format is one
edit here, not three.

Discord voice channels only accept audio, but many collections ship
lectures/interviews/etc. as video containers (mp4, mkv, mov) with a real
audio track inside. We accept those files too — FFmpeg will strip the
video stream (see `bot.player.default_ffmpeg_source` which passes `-vn`).
"""

from __future__ import annotations

# ---------------------------------------------------------------------- audio
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".m4a",
        ".m4b",  # audiobook
        ".mp4a",
        ".opus",
        ".ogg",
        ".oga",
        ".flac",
        ".wav",
        ".aac",
        ".aif",
        ".aiff",
        ".wma",
        ".mka",  # Matroska audio
        ".ac3",
        ".eac3",
        ".dts",
        ".amr",
        ".ape",
        ".alac",
        ".caf",
        ".dsf",
        ".dff",
        ".wv",
        ".tta",
        ".tak",
        ".mid",
        ".midi",
    }
)

# ---------------------------------------------------------------------- video
# Video containers that typically hold an audio track we can extract.
VIDEO_EXTS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".wmv",
        ".flv",
        ".mpg",
        ".mpeg",
        ".ts",
        ".3gp",
        ".m2ts",
        ".mts",
        ".m2v",
        ".vob",
        ".ogv",
        ".ogm",
        ".asf",
        ".rm",
        ".rmvb",
        ".3g2",
        ".divx",
        ".mxf",
    }
)

PLAYABLE_EXTS: frozenset[str] = AUDIO_EXTS | VIDEO_EXTS

# ---------------------------------------------------- archive.org "format" tags
# archive.org tags files with a human-ish format string. Keep both mp3 and
# video containers here — if the tag matches we accept the file even when the
# extension is weird.
AUDIO_ARCHIVE_FORMATS: frozenset[str] = frozenset(
    {
        "VBR MP3",
        "MP3",
        "128Kbps MP3",
        "64Kbps MP3",
        "Ogg Vorbis",
        "Ogg Video",  # some items mis-tag audio
        "FLAC",
        "Apple Lossless Audio",
        "Apple MPEG-4 Audio",
        "AIFF",
        "WAVE",
        "Wave",
    }
)

VIDEO_ARCHIVE_FORMATS: frozenset[str] = frozenset(
    {
        "MPEG4",
        "h.264",
        "h.264 HD",
        "512Kb MPEG4",
        "HiRes MPEG4",
        "Matroska",
        "Ogg Video",
        "QuickTime",
        "Windows Media",
        "AVI",
        "Cinepack",
    }
)

PLAYABLE_ARCHIVE_FORMATS: frozenset[str] = AUDIO_ARCHIVE_FORMATS | VIDEO_ARCHIVE_FORMATS

# ------------------------------------------------------- telegram mime prefixes
# Telethon exposes `document.mime_type` — e.g. "audio/mpeg", "video/mp4".
PLAYABLE_MIME_PREFIXES: tuple[str, ...] = ("audio/", "video/")


# ---------------------------------------------------------------- classifier
def is_video_ext(ext: str) -> bool:
    """True if `ext` (leading dot) is a video container."""
    return ext.lower() in VIDEO_EXTS


def is_playable_ext(ext: str) -> bool:
    return ext.lower() in PLAYABLE_EXTS


def is_video_mime(mime: str) -> bool:
    return (mime or "").lower().startswith("video/")


def is_playable_mime(mime: str) -> bool:
    m = (mime or "").lower()
    return any(m.startswith(p) for p in PLAYABLE_MIME_PREFIXES)
