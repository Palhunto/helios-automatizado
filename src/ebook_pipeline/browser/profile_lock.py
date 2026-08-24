from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, BinaryIO

from ebook_pipeline.core.errors import ConflictError


class BrowserProfileLock:
    """Process-scoped lock that serializes Hélios access to one browser profile."""

    def __init__(self, profile_dir: Path) -> None:
        self.path = profile_dir / "helios-playwright.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._lock(handle)
        except OSError as exc:
            handle.close()
            raise ConflictError(
                "BROWSER_PROFILE_IN_USE",
                "The dedicated browser profile is already locked by another Hélios process",
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            self._unlock(handle)
        finally:
            handle.close()
            self._handle = None

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl: Any = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl: Any = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
