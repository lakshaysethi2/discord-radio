"""Human-readable track titles for Discord (never raw provider hashes)."""

from __future__ import annotations

import os
import re

_ID_RE = re.compile(r"^(?:webdav|archive|local|telegram)_[0-9a-f]{8,}$", re.I)
_SPLIT_RE = re.compile(r"[-_]+")
_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.I)
_SMALL = frozenset({"and", "of", "the", "vs", "a", "an", "to", "for", "in", "on"})


def display_title(raw: str | None) -> str:
    """Turn a stored title or path into something a listener can read.

    Provider hashes like ``webdav_e40bd8be54970f5a`` are never shown.
    Hyphenated Drive stems become title case (``volume-ii-…`` →
    ``Volume II Consciousness and Addiction``).
    """
    text = (raw or "").strip()
    if not text or _ID_RE.fullmatch(text):
        return "Unknown track"
    leaf = text.rsplit("/", 1)[-1]
    stem, _ext = os.path.splitext(leaf)
    stem = stem.replace("--", " — ")
    parts = [p for p in _SPLIT_RE.split(stem) if p]
    if not parts:
        return "Unknown track"
    out: list[str] = []
    for i, part in enumerate(parts):
        low = part.lower()
        if _ROMAN_RE.fullmatch(part):
            out.append(part.upper())
        elif i > 0 and low in _SMALL:
            out.append(low)
        elif part.isupper() and len(part) <= 4:
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return " ".join(out)
