from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.academic.parsers import ANSWER_HEADER, ANSWER_TITLES
from ebook_pipeline.academic.validators import canonical_questions
from ebook_pipeline.cli import main
from ebook_pipeline.core.hashing import sha256_file
from ebook_pipeline.pagination.models import RendererFingerprint
from ebook_pipeline.pagination.service import PaginationService
from ebook_pipeline.prompts import PromptRegistry
from ebook_pipeline.writing.models import SubmissionDisposition


def _ready_canonical_consolidation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    *,
    contract_id: str = "omega_writing_production",
    character_target: int = 9200,
) -> str:
    project = services.projects.create(project_config_path)
    prompt = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    questionnaire = "\n".join(
        f"{number}. {question}"
        for number, question in enumerate(canonical_questions(prompt), start=1)
    ).encode()
    answer_lines = [ANSWER_HEADER]
    for number, title in enumerate(ANSWER_TITLES, start=1):
        answer_lines.extend(
            (f"{number}. {title}", f"[CONFIRMADO] Resposta consolidada {number}.")
        )
    services.academic.import_questionnaire(project.id, questionnaire)
    services.academic.import_answers(project.id, "\n".join(answer_lines).encode())
    services.academic.authorize_plan(project.id)
    services.academic.import_plan(project.id, b"# Plano academico\nDesenvolvimento aprovado.")
    context = services.writing.create_context(
        project.id,
        contract_id=contract_id,
        contract_version=1,
    )
    acknowledgement = services.writing.acknowledge_context(
        project.id, b"Resumo em quatro linhas.\nContexto avaliado e compreendido."
    )
    services.writing.confirm_context(project.id, acknowledgement.raw_version)
    contract = services.writing.contexts.resolve_contract(context)
    for unit in contract.model.ordered_units():
        services.writing.prepare_unit(project.id, unit.unit_id)
        suffix = (
            "\n\nExercícios analíticos e aplicados\n\n"
            "Questão analítica aplicada e contextualizada."
            if unit.unit_id.endswith("_B")
            else ""
        )
        stem = (
            f"Discussão acadêmica da unidade {unit.unit_id} articula ação, teoria, prática e "
            "evidência contextual com precisão conceitual. "
        )
        body = (stem * ((character_target // len(stem)) + 2))[: character_target - len(suffix)]
        submission = services.writing.import_unit(
            project.id, unit.unit_id, (body + suffix).encode()
        )
        if submission.disposition is SubmissionDisposition.REVIEW_REQUIRED:
            submission = services.writing.confirm_unit(
                project.id, unit.unit_id, submission.raw_version
            )
        assert submission.disposition is SubmissionDisposition.ACCEPTED, (
            services.writing.unit_validation_report(project.id, submission)
        )
    consolidation = services.writing.consolidate(project.id)
    assert consolidation.text_artifact_id is not None
    return project.id


def test_canonical_pagination_flow_is_idempotent_and_valid(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _ready_canonical_consolidation(
        services, project_config_path, repository_root
    )

    first = services.pagination.create(project_id)
    monkeypatch.setattr(
        services.pagination.renderer,
        "render",
        lambda *_args, **_kwargs: pytest.fail("idempotent create must not launch Chromium"),
    )
    second = services.pagination.create(project_id)
    shown, manifest = services.pagination.show(project_id)
    status = services.pagination.status(project_id)

    assert second.id == first.id == shown.id
    assert second.stage_run_id == first.stage_run_id
    assert first.document_page_count == len(manifest["pages"])
    assert first.eligible_page_count > 8
    assert manifest["schema"] == "helios_pagination_snapshot@1"
    assert manifest["layout"]["chapter_boundary"] == {
        "chapter_units_start_new_page": True,
        "inject_visible_heading": False,
        "visible_heading_policy": "source_only",
    }
    eligible = [page for page in manifest["pages"] if page["eligible"]]
    assert all(page["visual_slot"] is not None for page in eligible)
    assert any(len(page["unit_spans"]) == 2 for page in eligible)
    assert status["current"] is True
    assert status["stale"] is False
    assert services.pagination.validate(project_id) == []
    current_fingerprint = services.pagination.renderer.fingerprint()
    changed_fingerprint = RendererFingerprint(
        fingerprint="f" * 64,
        os_name=current_fingerprint.os_name,
        os_release=current_fingerprint.os_release,
        os_version=current_fingerprint.os_version,
        machine=current_fingerprint.machine,
        python_version=current_fingerprint.python_version,
        playwright_version=current_fingerprint.playwright_version,
        pypdf_version=current_fingerprint.pypdf_version,
        chromium_executable_sha256=current_fingerprint.chromium_executable_sha256,
    )
    monkeypatch.setattr(
        services.pagination.renderer, "fingerprint", lambda: changed_fingerprint
    )
    stale = services.pagination.status(project_id)
    assert stale["current"] is False
    assert stale["stale_reasons"] == ["renderer_fingerprint_changed"]
    monkeypatch.undo()

    with services.database.connection() as connection:
        counts = {
            "snapshots": connection.execute(
                "SELECT count(*) FROM visual_pagination_snapshots WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0],
            "runs": connection.execute(
                "SELECT count(*) FROM stage_runs WHERE project_id = ? "
                "AND stage_id = 'visual_pagination'",
                (project_id,),
            ).fetchone()[0],
            "artifacts": connection.execute(
                "SELECT count(*) FROM artifacts WHERE project_id = ? "
                "AND artifact_type LIKE 'pagination_%'",
                (project_id,),
            ).fetchone()[0],
        }
    assert counts == {"snapshots": 1, "runs": 1, "artifacts": 3}

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
    assert main([*base, "pagination", "create", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == first.id
    assert main([*base, "pagination", "show", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"]["snapshot_id"] == first.id
    assert main([*base, "pagination", "status", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["current"] is True
    assert main([*base, "pagination", "validate", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_canonical_pagination_recovers_same_identity_after_pdf_checkpoint(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_canonical_consolidation(
        services, project_config_path, repository_root
    )
    failed = False

    def fail_after_pdf(checkpoint: str) -> None:
        nonlocal failed
        if checkpoint == "pagination.pdf" and not failed:
            failed = True
            raise RuntimeError("simulated crash after canonical PDF")

    crashing = PaginationService(
        services.config,
        services.database,
        services.store,
        services.writing,
        fault_hook=fail_after_pdf,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.create(project_id)
    project = services.projects.get(project_id)
    interrupted_pdf = services.store.resolve(
        project.artifact_root, "visual-plan/pagination/v0001/document.pdf"
    )
    interrupted_pdf_sha256 = sha256_file(interrupted_pdf)
    with services.database.connection() as connection:
        interrupted = connection.execute(
            "SELECT s.id, r.status, r.attempt FROM visual_pagination_snapshots s "
            "JOIN stage_runs r ON r.id = s.stage_run_id WHERE s.project_id = ?",
            (project_id,),
        ).fetchone()
        assert interrupted is not None
        assert interrupted["status"] == "pending_retry"
        assert interrupted["attempt"] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE project_id = ? "
                "AND artifact_type LIKE 'pagination_%'",
                (project_id,),
            ).fetchone()[0]
            == 0
        )

    recovered = services.pagination.create(project_id)
    assert sha256_file(interrupted_pdf) == interrupted_pdf_sha256
    assert recovered.id == interrupted["id"]
    with services.database.connection() as connection:
        run = connection.execute(
            "SELECT status, attempt FROM stage_runs WHERE id = ?",
            (recovered.stage_run_id,),
        ).fetchone()
        assert run is not None
        assert dict(run) == {"status": "done", "attempt": 2}
    assert services.pagination.validate(project_id) == []
