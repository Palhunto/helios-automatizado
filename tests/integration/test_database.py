from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.storage.database import Database


def test_open_failure_is_translated_to_domain_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    with pytest.raises(IntegrityError) as captured:
        Database(blocker / "helios.db").connect()
    assert captured.value.code == "DATABASE_OPEN_FAILED"


def test_transaction_rolls_back_and_connection_remains_usable(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        with pytest.raises(RuntimeError, match="abort"), database.transaction(connection):
            connection.execute(
                "INSERT INTO projects(id, name, slug, config_path, artifact_root, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("id", "Project", "project", "config.yaml", "project-id", "now", "now"),
            )
            raise RuntimeError("abort")
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        with database.transaction(connection):
            connection.execute(
                "INSERT INTO projects(id, name, slug, config_path, artifact_root, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("id", "Project", "project", "config.yaml", "project-id", "now", "now"),
            )
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def test_connection_context_closes_after_exception(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    connection: sqlite3.Connection | None = None
    with pytest.raises(RuntimeError), database.connection() as active:
        connection = active
        raise RuntimeError("stop")
    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_connect_without_migrations_keeps_connection_pragmas(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    connection = database.connect(migrate=False)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_read_only_connection_cannot_modify_operational_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection, database.transaction(connection):
        connection.execute(
            "INSERT INTO projects(id, name, slug, config_path, artifact_root, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("id", "Project", "project", "config.yaml", "project-id", "now", "now"),
        )

    before = database.path.read_bytes()
    with database.read_only_connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM projects")
    assert database.path.read_bytes() == before
