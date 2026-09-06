from pathlib import Path

from ebook_pipeline.storage.database import Database


def test_m4_0_migration_entities_remain_present(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'visual_%'"
            ).fetchall()
        }
        assert {
            "visual_pagination_snapshots",
            "visual_pagination_pages",
            "visual_pagination_page_unit_spans",
        } <= tables
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 7"
        ).fetchone()
        assert migration is not None
        assert migration[0] == "0007_m4_canonical_pagination.sql"


def test_pagination_page_schema_enforces_eligible_page_binding(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'visual_pagination_pages'"
            ).fetchone()[0]
        )
        assert "page_key = chapter_id || '-P' || printf('%03d', chapter_page_number)" in sql
        assert "chapter_page_number IS NOT NULL AND page_key IS NOT NULL" in sql
        assert "eligible = 0 AND eligible_page_number IS NULL" in sql


def test_pagination_span_schema_binds_page_and_project_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'visual_pagination_page_unit_spans'"
            ).fetchone()[0]
        )
        normalized = " ".join(sql.split())
        assert (
            "FOREIGN KEY (pagination_snapshot_id, project_id, document_page_number) "
            "REFERENCES visual_pagination_pages( pagination_snapshot_id, project_id, "
            "document_page_number )"
        ) in normalized
