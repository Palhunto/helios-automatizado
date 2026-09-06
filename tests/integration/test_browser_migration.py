from __future__ import annotations

from pathlib import Path

from ebook_pipeline.storage.database import Database


def test_m3_migration_creates_projection_and_browser_contracts(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    expected = {
        "writing_context_projection_manifests",
        "writing_unit_academic_projections",
        "browser_conversations",
        "browser_interactions",
        "browser_interaction_events",
        "browser_interaction_resolutions",
        "browser_conversation_invalidations",
    }
    with database.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert expected <= tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(range(1, 10))

    with database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 4"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 5"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0] == 1
