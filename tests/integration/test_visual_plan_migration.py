from pathlib import Path

from ebook_pipeline.storage.database import Database


def test_m4_migrations_contain_only_visual_planning_entities(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'visual_%'"
            ).fetchall()
        }
        assert tables == {
            "visual_pagination_snapshots",
            "visual_pagination_pages",
            "visual_pagination_page_unit_spans",
            "visual_plans",
            "visual_figures",
            "visual_figure_page_units",
            "visual_anchors",
            "visual_finalizations",
            "visual_finalization_anchors",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 8"
        ).fetchone()
        assert migration is not None
        assert migration[0] == "0008_m4_visual_plan_import.sql"


def test_m4_1_schema_has_no_anchor_or_accepted_manifest_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "helios.db")
    with database.connection() as connection:
        plan_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(visual_plans)").fetchall()
        }
        figure_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(visual_figures)").fetchall()
        }
        assert "accepted_manifest_artifact_id" not in plan_columns
        assert "accepted_version" not in plan_columns
        assert "unit_id" not in figure_columns
        assert "resolved_unit_id" not in figure_columns
