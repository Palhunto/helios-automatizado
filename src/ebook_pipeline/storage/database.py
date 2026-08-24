from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.storage.migrations import apply_migrations


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self, *, migrate: bool = True) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            if migrate:
                apply_migrations(connection)
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise IntegrityError("DATABASE_OPEN_FAILED", f"Could not open database: {exc}") from exc

    def connect_read_only(self) -> sqlite3.Connection:
        """Open an existing database without migrations, WAL changes, or write capability."""
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                isolation_level=None,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise IntegrityError(
                "DATABASE_READ_ONLY_OPEN_FAILED",
                f"Could not open database read-only: {exc}",
            ) from exc

    @contextmanager
    def connection(self, *, migrate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect(migrate=migrate)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def read_only_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect_read_only()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    @contextmanager
    def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
