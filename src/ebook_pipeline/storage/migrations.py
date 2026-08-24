from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

from ebook_pipeline.core.errors import MigrationError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.ids import utc_now

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _migration_files() -> list[Path]:
    directory = resources.files("ebook_pipeline.storage").joinpath("sql")
    return sorted(
        Path(str(item))
        for item in directory.iterdir()
        if item.name.endswith(".sql") and item.name[:4].isdigit()
    )


def _statements(script: str) -> Iterator[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise MigrationError("MIGRATION_SQL_INCOMPLETE", "Migration ends with incomplete SQL")


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(BOOTSTRAP_SQL)
    for path in _migration_files():
        version = int(path.name[:4])
        content = path.read_bytes()
        digest = sha256_bytes(content)
        existing = connection.execute(
            "SELECT name, sha256 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["name"] != path.name or existing["sha256"] != digest:
                raise MigrationError(
                    "MIGRATION_DRIFT",
                    f"Applied migration {version} differs from {path.name}",
                    evidence={"version": version, "path": path.name},
                )
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _statements(content.decode("utf-8")):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (version, path.name, digest, utc_now()),
            )
            connection.commit()
        except (sqlite3.Error, UnicodeDecodeError) as exc:
            connection.rollback()
            raise MigrationError(
                "MIGRATION_FAILED", f"Could not apply migration {path.name}: {exc}"
            ) from exc
