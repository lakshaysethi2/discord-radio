"""Disk quota helpers for provider-managed storage.

The playback cache and torrent data live in different directories, but they
share the provider's disk budget. This module counts actual inodes so a
hardlinked torrent file in the playback cache is not charged twice.
"""

from __future__ import annotations

from pathlib import Path


class StorageQuota:
    """A conservative quota over a set of provider-owned paths."""

    def __init__(self, max_bytes: int, roots: list[Path]) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self.roots = tuple(Path(root) for root in roots)

    def usage_bytes(self) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for root in self.roots:
            if root.is_file():
                total += self._file_size(root, seen)
                continue
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    total += self._file_size(path, seen)
        return total

    @staticmethod
    def _file_size(path: Path, seen: set[tuple[int, int]]) -> int:
        try:
            stat = path.stat()
        except OSError:
            return 0
        inode = (stat.st_dev, stat.st_ino)
        if inode in seen:
            return 0
        seen.add(inode)
        return stat.st_size

    def free_bytes(self) -> int:
        if self.max_bytes == 0:
            return 0
        return max(0, self.max_bytes - self.usage_bytes())

    def projected_bytes(self, additional_bytes: int = 0) -> int:
        return self.usage_bytes() + max(0, int(additional_bytes))

    def allows(self, additional_bytes: int = 0) -> bool:
        # A zero quota is treated as an explicit zero-byte limit, not
        # "unlimited". Config defaults to a positive value.
        return self.projected_bytes(additional_bytes) <= self.max_bytes
