from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ebook_pipeline.core.errors import ConfigurationError

WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def validate_relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value or DRIVE_PREFIX.match(value):
        raise ConfigurationError("PATH_INVALID", f"Unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigurationError("PATH_INVALID", f"Unsafe relative path: {value!r}")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED or part.endswith((" ", ".")) or ":" in part:
            raise ConfigurationError("PATH_INVALID", f"Unsafe path component: {part!r}")
    return path.as_posix()


def resolve_under(root: Path, relative_path: str) -> Path:
    normalized = validate_relative_path(relative_path)
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*PurePosixPath(normalized).parts)).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ConfigurationError(
            "PATH_OUTSIDE_ROOT", f"Path escapes configured root: {relative_path}"
        )
    return candidate
