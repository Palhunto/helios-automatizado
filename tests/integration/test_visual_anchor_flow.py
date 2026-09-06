from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from conftest import ServiceBundle
from jsonschema import Draft202012Validator
from test_pagination_flow import _ready_canonical_consolidation
from test_visual_plan_flow import _raw_plan

from ebook_pipeline.cli import main
from ebook_pipeline.core.errors import ConflictError, IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.pagination.service import PaginationService
from ebook_pipeline.storage import migrations
from ebook_pipeline.visual_planning.anchor_repository import AnchorRepository
from ebook_pipeline.visual_planning.enrichment import VisualAnchorService
from ebook_pipeline.visual_planning.finalization import VisualFinalizationService
from ebook_pipeline.visual_planning.service import VisualPlanningService
from ebook_pipeline.writing.service import WritingService


@pytest.fixture
def services(services: ServiceBundle, repository_root: Path, tmp_path: Path) -> ServiceBundle:
    # Keep all 18 units and real page measurement; shorten only the synthetic test contract.
    content = (repository_root / "writing_contracts/omega_writing_v1.yaml").read_text("utf-8")
    content = (
        content.replace("id: omega_writing_production", "id: test_visual_writing")
        .replace("min_characters: 9000", "min_characters: 700")
        .replace("max_characters: 10000", "max_characters: 1000")
    )
    contract = tmp_path / "compact.yaml"
    contract.write_text(content, encoding="utf-8")
    registry = tmp_path / "writing-registry.yaml"
    registry.write_text(
        "contracts:\n  - id: test_visual_writing\n    version: 1\n    status: active\n"
        f"    path: compact.yaml\n    sha256: {sha256_bytes(contract.read_bytes())}\n",
        encoding="utf-8",
    )
    config = services.config.model_copy(update={"writing_contract_registry": registry})
    writing = WritingService(config, services.database, services.store, services.academic)
    pagination = PaginationService(config, services.database, services.store, writing)
    visual = VisualPlanningService(config, services.database, services.store, pagination)
    return replace(services, config=config, writing=writing, pagination=pagination, visual=visual)


def _ready(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> tuple[str, dict[str, Any], str]:
    project_id = _ready_canonical_consolidation(
        services,
        project_config_path,
        repository_root,
        contract_id="test_visual_writing",
        character_target=800,
    )
    services.pagination.create(project_id)
    _, pagination = services.pagination.show(project_id)
    services.visual.import_raw(project_id, _raw_plan(pagination))
    _, _, _, text = services.visual.require_plan(project_id)
    return project_id, pagination, text


def _candidate(page: dict[str, Any], text: str, *, before: bool = False) -> bytes:
    span = page["unit_spans"][-1]
    return json.dumps(
        {
            "anchor_text": text[span["global_char_start"] : span["global_char_end"]],
            "position_relative_to_anchor": "before" if before else "after",
        },
        ensure_ascii=False,
    ).encode()


def _populate(
    services: ServiceBundle, project_id: str, pagination: dict[str, Any], text: str
) -> None:
    anchors = VisualAnchorService(services.visual)
    _, figures = services.visual.list_figures(project_id)
    pages = [p for p in pagination["pages"] if p["eligible"]]
    for figure, page in zip(figures, pages, strict=True):
        anchor, report = anchors.import_raw(project_id, figure.id, _candidate(page, text))
        assert anchor.disposition == "valid", report
        assert anchor.unit_id == page["unit_spans"][-1]["unit_id"]


def test_visual_finalize_restart_idempotence_repair_and_history(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    anchors = VisualAnchorService(services.visual)
    finalizations = VisualFinalizationService(services.visual)
    _, figures = services.visual.list_figures(project_id)
    editorial_before = [asdict(f) for f in figures]
    with pytest.raises(ConflictError, match="Every figure"):
        finalizations.finalize(project_id)
    _populate(services, project_id, pagination, text)
    first, manifest = finalizations.finalize(project_id)
    assert manifest["schema"] == "helios_visual_manifest@1"
    manifest_schema = json.loads(
        (repository_root / "schemas/visual_manifest.schema.json").read_bytes()
    )
    Draft202012Validator(manifest_schema).validate(json.loads(json.dumps(manifest)))
    final_schema = json.loads(
        (repository_root / "schemas/visual_finalization.schema.json").read_bytes()
    )
    Draft202012Validator(final_schema).validate(asdict(first))
    assert manifest["figure_count"] == pagination["eligible_page_count"]
    assert [f["page_key"] for f in manifest["figures"]] == [
        p["page_key"] for p in pagination["pages"] if p["eligible"]
    ]
    project = services.projects.get(project_id)
    output = services.store.resolve(project.artifact_root, "visual-plan/accepted/v0001.json")
    original = output.read_bytes()
    restarted = VisualPlanningService(
        services.config, services.database, services.store, services.pagination
    )
    finalizations = VisualFinalizationService(restarted)
    second, repeated = finalizations.finalize(project_id)
    assert first == second
    assert manifest == repeated
    assert finalizations.status(project_id)["current"] is True
    bad, report = anchors.import_raw(project_id, figures[0].id, b'{"anchor_text":"missing"}')
    assert bad.disposition == "invalid"
    assert report["validation"]["code"] == "VISUAL_ANCHOR_PAYLOAD_INVALID"
    assert finalizations.status(project_id)["current"] is False
    with pytest.raises(ConflictError):
        finalizations.finalize(project_id)
    page = next(p for p in pagination["pages"] if p["eligible"])
    repaired, _ = anchors.import_raw(project_id, figures[0].id, _candidate(page, text, before=True))
    assert repaired.version == 3
    assert repaired.supersedes_anchor_id == bad.id
    third, _ = finalizations.finalize(project_id)
    assert third.accepted_version == 2
    assert output.read_bytes() == original
    assert finalizations.show(project_id, 1)[1] == manifest
    assert [asdict(f) for f in services.visual.list_figures(project_id)[1]] == editorial_before
    assert services.visual.validate(project_id) == []
    assert finalizations.validate(project_id) == []
    with services.database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM visual_finalizations").fetchone()[0] == 2


@pytest.mark.parametrize("checkpoint", ["visual_anchor.raw", "visual_anchor.validation"])
def test_anchor_checkpoint_recovery_uses_durable_raw_and_preserves_bytes(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    checkpoint: str,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    figure = services.visual.list_figures(project_id)[1][0]
    page = next(p for p in pagination["pages"] if p["eligible"])
    raw = _candidate(page, text)
    anchors = VisualAnchorService(services.visual)

    def fail(name: str) -> None:
        if name == checkpoint:
            raise RuntimeError("injected crash")

    services.visual.fault_hook = fail
    with pytest.raises(RuntimeError, match="injected crash"):
        anchors.import_raw(project_id, figure.id, raw)
    services.visual.fault_hook = None
    restored = VisualAnchorService(
        VisualPlanningService(
            services.config, services.database, services.store, services.pagination
        )
    )
    anchor, report = restored.recover(project_id, figure.id)
    assert anchor.version == 1 and anchor.disposition == "valid"
    assert anchor.raw_sha256 == sha256_bytes(raw)
    assert restored.recover(project_id, figure.id) == (anchor, report)
    with services.database.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT status, attempt FROM stage_runs WHERE id = ?", (anchor.stage_run_id,)
            ).fetchone()
        ) == ("done", 2)


def test_finalize_checkpoint_recovery_and_manifest_corruption(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    _populate(services, project_id, pagination, text)
    finalizations = VisualFinalizationService(services.visual)

    def fail(name: str) -> None:
        if name == "visual_finalize.manifest":
            raise RuntimeError("injected crash")

    services.visual.fault_hook = fail
    with pytest.raises(RuntimeError):
        finalizations.finalize(project_id)
    project = services.projects.get(project_id)
    output = services.store.resolve(project.artifact_root, "visual-plan/accepted/v0001.json")
    raw = output.read_bytes()
    services.visual.fault_hook = None
    item, _ = finalizations.finalize(project_id)
    assert item.accepted_version == 1
    assert output.read_bytes() == raw
    output.write_bytes(b"corrupted")
    with pytest.raises(IntegrityError):
        finalizations.finalize(project_id)
    assert output.read_bytes() == b"corrupted"
    assert finalizations.status(project_id)["current"] is False
    assert finalizations.validate(project_id)


def test_visual_provenance_tampering_and_stale_plan_block_anchor_import(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    figure = services.visual.list_figures(project_id)[1][0]
    page = next(p for p in pagination["pages"] if p["eligible"])
    with services.database.connection() as connection:
        connection.execute("UPDATE visual_plans SET production_set_hash = ?", ("f" * 64,))
    with pytest.raises(IntegrityError, match="provenance"):
        VisualAnchorService(services.visual).import_raw(
            project_id, figure.id, _candidate(page, text)
        )
    with services.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM visual_anchors").fetchone()[0] == 0


def test_visual_anchor_cli_and_source_corruption_fail_closed(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    figure = services.visual.list_figures(project_id)[1][0]
    page = next(p for p in pagination["pages"] if p["eligible"])
    raw = _candidate(page, text)
    path = tmp_path / "anchor.json"
    path.write_bytes(raw)
    base = [
        "--data-dir",
        str(services.config.data_dir),
        "--projects-dir",
        str(services.config.projects_dir),
        "--writing-contract-registry",
        str(services.config.writing_contract_registry),
    ]
    assert main([*base, "visual", "anchor", "request", project_id, figure.id]) == 0
    assert "helios_visual_anchor_enrichment@1" in json.loads(capsys.readouterr().out)["request"]
    assert main([*base, "visual", "anchor", "import", project_id, figure.id, str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    for key, filename in (
        ("anchor", "visual_anchor.schema.json"),
        ("validation_report", "visual_anchor_validation.schema.json"),
    ):
        schema = json.loads((repository_root / "schemas" / filename).read_bytes())
        Draft202012Validator(schema).validate(result[key])
    assert main([*base, "visual", "anchor", "show", project_id, figure.id]) == 0
    assert json.loads(capsys.readouterr().out) == result
    with services.database.connection() as connection:
        anchor = AnchorRepository(connection).get(result["anchor"]["id"])
        row = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = ?", (anchor.raw_artifact_id,)
        ).fetchone()
    project = services.projects.get(project_id)
    services.store.resolve(project.artifact_root, row[0]).write_bytes(b"changed")
    with pytest.raises(IntegrityError):
        VisualAnchorService(services.visual).show(project_id, figure.id)


def test_m4_2_migration_preserves_populated_m4_1_tables(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = migrations._migration_files()  # noqa: SLF001
    monkeypatch.setattr(migrations, "_migration_files", lambda: files[:8])
    project_id, pagination, _ = _ready(services, project_config_path, repository_root)
    with services.database.connection() as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name != 'schema_migrations'"
            )
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            for table in tables
        }
        baseline = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")]
    monkeypatch.setattr(migrations, "_migration_files", lambda: files)
    with services.database.connection() as connection:
        after = {
            table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            for table in tables
        }
        assert before == after
        assert baseline == [
            tuple(row)
            for row in connection.execute("SELECT * FROM schema_migrations WHERE version <= 8")
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM visual_anchors").fetchone()[0] == 0


def test_anchor_rechecks_source_currentness_before_commit(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    figure = services.visual.list_figures(project_id)[1][0]
    page = next(p for p in pagination["pages"] if p["eligible"])

    def change_source(name: str) -> None:
        if name == "visual_anchor.validation":
            services.academic.import_plan(
                project_id, b"# Novo plano\nFonte alterada durante import."
            )

    services.visual.fault_hook = change_source
    with pytest.raises(ConflictError, match="stale"):
        VisualAnchorService(services.visual).import_raw(
            project_id, figure.id, _candidate(page, text)
        )
    with services.database.connection() as connection:
        anchor = AnchorRepository(connection).latest(figure.id)
        assert anchor is not None
        assert anchor.disposition == "processing" and anchor.raw_artifact_id is None
    assert services.visual.status(project_id)["current"] is False


def test_anchor_output_mutation_at_commit_never_promotes_corrupt_evidence(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, pagination, text = _ready(services, project_config_path, repository_root)
    figure = services.visual.list_figures(project_id)[1][0]
    page = next(p for p in pagination["pages"] if p["eligible"])
    project = services.projects.get(project_id)
    raw_path = services.store.resolve(
        project.artifact_root, f"visual-plan/anchors/{figure.id}/v0001/raw.json"
    )

    def corrupt(name: str) -> None:
        if name == "visual_anchor.validation":
            raw_path.write_bytes(b"corrupt source")

    services.visual.fault_hook = corrupt
    with pytest.raises(IntegrityError, match="changed before commit"):
        VisualAnchorService(services.visual).import_raw(
            project_id, figure.id, _candidate(page, text)
        )
    with services.database.connection() as connection:
        anchor = AnchorRepository(connection).latest(figure.id)
        assert anchor is not None and anchor.disposition == "processing"
        assert anchor.raw_artifact_id is None
    services.visual.fault_hook = None
    with pytest.raises(IntegrityError, match="hash differs"):
        VisualAnchorService(services.visual).recover(project_id, figure.id)
    assert raw_path.read_bytes() == b"corrupt source"
