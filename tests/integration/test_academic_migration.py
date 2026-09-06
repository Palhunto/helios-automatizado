from pathlib import Path

from ebook_pipeline.storage.database import Database


def test_m1_migration_creates_relational_contract_and_reopens(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    expected = {
        "academic_documents",
        "academic_plan_authorizations",
        "academic_review_decisions",
    }
    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert expected <= tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        migrations = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row[0] for row in migrations] == list(range(1, 10))
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
