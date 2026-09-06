from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ServiceBundle
from test_pagination_flow import _ready_canonical_consolidation

from ebook_pipeline.cli import main


def _raw_plan(manifest: dict[str, Any]) -> bytes:
    pages = [page for page in manifest["pages"] if page["eligible"]]
    blocks: list[str] = []
    for number, page in enumerate(pages, start=1):
        page_key = page["page_key"]
        blocks.append(
            f"""Imagem {number} — Figura editorial {number}

Página: {page_key}
Seção: Campo editorial que não vincula unidade
Posição exata no texto: Após o argumento principal
Conceito principal a representar: Conceito {number}
Síntese conceitual da imagem: Síntese integral {number}
Justificativa curta: Justificativa {number}
Objetivo da imagem: Explicar o conceito {number}
Tipo de imagem sugerido: Diagrama editorial
Complexidade visual: Editorial estruturada
Prompt independente para geração: Prompt completo {number}
com segunda linha preservada"""
        )
    return ("\r\n\r\n".join(blocks) + "\r\n").encode()


def _ready_snapshot(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> tuple[str, dict[str, Any]]:
    project_id = _ready_canonical_consolidation(
        services, project_config_path, repository_root
    )
    services.pagination.create(project_id)
    _, manifest = services.pagination.show(project_id)
    return project_id, manifest


def test_visual_plan_import_is_idempotent_and_preserves_late_unit_resolution(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id, manifest = _ready_snapshot(
        services, project_config_path, repository_root
    )
    raw = _raw_plan(manifest)

    first, report = services.visual.import_raw(project_id, raw)
    second, second_report = services.visual.import_raw(project_id, raw)
    shown, figures, shown_report, shown_raw = services.visual.show(project_id)

    assert first.id == second.id == shown.id
    assert first.disposition == "valid"
    assert first.figure_count == first.expected_figure_count
    assert report == second_report == shown_report
    assert shown_raw == raw
    assert services.visual.validate(project_id) == []
    assert services.visual.status(project_id)["current"] is True

    eligible_pages = [page for page in manifest["pages"] if page["eligible"]]
    assert len(figures) == len(eligible_pages)
    for figure, page in zip(figures, eligible_pages, strict=True):
        expected_units = tuple(
            dict.fromkeys(span["unit_id"] for span in page["unit_spans"])
        )
        assert figure.page_unit_ids == expected_units
        assert figure.section == "Campo editorial que não vincula unidade"
        assert not hasattr(figure, "unit_id")
        assert figure.generation_prompt.endswith("com segunda linha preservada")

    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM visual_plans WHERE project_id = ?", (project_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM stage_runs WHERE project_id = ? "
            "AND stage_id = 'visual_plan_import'",
            (project_id,),
        ).fetchone()[0] == 1

    raw_path = tmp_path / "visual-plan-v2.txt"
    raw_path.write_bytes(raw)
    base = [
        "--data-dir",
        str(services.config.data_dir),
        "--projects-dir",
        str(services.config.projects_dir),
        "--pipeline-config",
        str(services.config.pipeline_config),
        "--prompt-registry",
        str(services.config.prompt_registry),
        "--writing-contract-registry",
        str(services.config.writing_contract_registry),
        "--pagination-layout-registry",
        str(services.config.pagination_layout_registry),
    ]
    assert main([*base, "visual", "plan", "import", project_id, str(raw_path)]) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["id"] == first.id
    assert main([*base, "visual", "plan", "show", project_id, "--raw"]) == 0
    assert json.loads(capsys.readouterr().out)["raw"] == raw.decode()
    assert main([*base, "visual", "plan", "status", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["current"] is True
    assert main([*base, "visual", "plan", "validate", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main([*base, "visual", "figure", "list", project_id]) == 0
    listed = json.loads(capsys.readouterr().out)["figures"]
    assert len(listed) == first.figure_count
    assert main([*base, "visual", "figure", "show", project_id, listed[0]["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["page_unit_ids"] == listed[0]["page_unit_ids"]


def test_invalid_visual_plan_preserves_raw_and_report_without_partial_figures(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, manifest = _ready_snapshot(
        services, project_config_path, repository_root
    )
    raw = _raw_plan(manifest)
    first_page = next(page for page in manifest["pages"] if page["eligible"])
    invalid = raw.replace(str(first_page["page_key"]).encode(), b"CH08-P999", 1)

    plan, report = services.visual.import_raw(project_id, invalid)
    repeated, repeated_report = services.visual.import_raw(project_id, invalid)
    _, figures, _, preserved = services.visual.show(project_id)

    assert repeated.id == plan.id
    assert repeated_report == report
    assert plan.disposition == "invalid"
    assert preserved == invalid
    assert figures == []
    assert report["valid"] is False
    assert services.visual.status(project_id)["current"] is False
    assert {finding["code"] for finding in report["findings"]} >= {
        "VISUAL_PLAN_PAGE_MISSING",
        "VISUAL_PLAN_PAGE_INVENTED",
    }
    assert services.visual.validate(project_id) == []


def test_visual_plan_recovers_after_raw_checkpoint(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, manifest = _ready_snapshot(
        services, project_config_path, repository_root
    )
    raw = _raw_plan(manifest)
    fired = False

    def fail_once(checkpoint: str) -> None:
        nonlocal fired
        if checkpoint == "visual_plan.raw" and not fired:
            fired = True
            raise RuntimeError("injected failure")

    services.visual.fault_hook = fail_once
    with pytest.raises(RuntimeError, match="injected failure"):
        services.visual.import_raw(project_id, raw)
    project = services.projects.get(project_id)
    assert services.store.resolve(
        project.artifact_root, "visual-plan/raw/v0001.txt"
    ).read_bytes() == raw
    with services.database.connection() as connection:
        interrupted = connection.execute(
            "SELECT disposition, raw_artifact_id FROM visual_plans WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert tuple(interrupted) == ("processing", None)
    services.visual.fault_hook = None

    recovered, _ = services.visual.import_raw(project_id, raw)

    assert recovered.disposition == "valid"
    with services.database.connection() as connection:
        run = connection.execute(
            "SELECT status, attempt FROM stage_runs WHERE id = ?", (recovered.stage_run_id,)
        ).fetchone()
        assert tuple(run) == ("done", 2)
