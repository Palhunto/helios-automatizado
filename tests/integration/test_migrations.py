import sqlite3
from pathlib import Path

import pytest

from ebook_pipeline.core.errors import MigrationError
from ebook_pipeline.storage import migrations
from ebook_pipeline.storage.database import Database


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "helios.db")
    with database.connection() as connection:
        first = connection.execute("SELECT * FROM schema_migrations").fetchall()
        assert len(first) == 7
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with database.connection() as connection:
        second = connection.execute("SELECT * FROM schema_migrations").fetchall()
        assert len(second) == 7


def test_migration_drift_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = tmp_path / "0001_test.sql"
    migration.write_text("CREATE TABLE sample(id TEXT PRIMARY KEY);\n", encoding="utf-8")
    monkeypatch.setattr(migrations, "_migration_files", lambda: [migration])
    connection = sqlite3.connect(tmp_path / "drift.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    migrations.apply_migrations(connection)
    migration.write_text("CREATE TABLE changed(id TEXT PRIMARY KEY);\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="differs"):
        migrations.apply_migrations(connection)
    connection.close()


def test_invalid_migration_rolls_back_every_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = tmp_path / "0001_invalid.sql"
    migration.write_text(
        "CREATE TABLE partial_change(id TEXT PRIMARY KEY);\nTHIS IS INVALID SQL;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "_migration_files", lambda: [migration])
    connection = sqlite3.connect(tmp_path / "invalid.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    with pytest.raises(MigrationError) as captured:
        migrations.apply_migrations(connection)
    assert captured.value.code == "MIGRATION_FAILED"
    assert (
        connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'partial_change'"
        ).fetchone()[0]
        == 0
    )
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    connection.execute("CREATE TABLE database_still_usable(id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO database_still_usable(id) VALUES ('ok')")
    assert connection.execute("SELECT id FROM database_still_usable").fetchone()[0] == "ok"
    connection.close()


def test_incomplete_migration_statement_is_rejected() -> None:
    with pytest.raises(MigrationError) as captured:
        list(migrations._statements("CREATE TABLE unfinished("))  # noqa: SLF001
    assert captured.value.code == "MIGRATION_SQL_INCOMPLETE"
