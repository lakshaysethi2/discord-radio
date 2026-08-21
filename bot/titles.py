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
    global_idx = 0
    for part in parts:
        # ponytail: split on spaces so "Test Track" keeps both words titled
        for word in part.split():
            low = word.lower()
            if _ROMAN_RE.fullmatch(word):
                out.append(word.upper())
            elif global_idx > 0 and low in _SMALL:
                out.append(low)
            elif word.isupper() and len(word) <= 4:
                out.append(word)
            else:
                out.append(word[:1].upper() + word[1:].lower())
            global_idx += 1
    return " ".join(out)
