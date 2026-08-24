from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_academic_flow import answers_bytes, questionnaire_bytes

from ebook_pipeline.academic.models import DocumentKind
from ebook_pipeline.academic.service import ANSWERS_STAGE, PLAN_STAGE, QUESTIONNAIRE_STAGE
from ebook_pipeline.core.errors import ArtifactError, IntegrityError
from ebook_pipeline.core.models import Project, RunStatus, StageRun
from ebook_pipeline.storage.repositories import StageRunRepository


def _prepare(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> Project:
    project = services.projects.create(project_config_path)
    services.academic.import_questionnaire(project.id, questionnaire_bytes(repository_root))
    return project


def _running_answers(services: ServiceBundle, project_id: str) -> StageRun:
    run = [item for item in services.projects.runs(project_id) if item.stage_id == ANSWERS_STAGE][0]
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE id = ?",
            (run.id,),
        )
    return run


def _force_latest_running(
    services: ServiceBundle, project_id: str, stage_id: str, *, confirmation: bool = False
) -> StageRun:
    matching = [
        item
        for item in services.projects.runs(project_id)
        if item.stage_id == stage_id and item.unit_id.startswith("confirm:") is confirmation
    ]
    run = matching[-1]
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE id = ?",
            (run.id,),
        )
    return run


def test_academic_recovery_reconciles_promoted_outputs_idempotently(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)

    def crash_before_database(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash after promotion")

    monkeypatch.setattr(services.academic, "_finish_accepted", crash_before_database)
    with pytest.raises(IntegrityError):
        services.academic.import_answers(project.id, answers_bytes())
    _running_answers(services, project.id)
    with pytest.raises(IntegrityError) as same_running:
        services.academic.import_answers(project.id, answers_bytes())
    assert same_running.value.code == "ACADEMIC_RECOVERY_REQUIRED"
    with pytest.raises(IntegrityError) as newer_input:
        services.academic.import_answers(project.id, answers_bytes(suffix=" nova"))
    assert newer_input.value.code == "ACADEMIC_RECOVERY_REQUIRED"
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.DONE]
    assert services.academic.validate(project.id) == []
    assert services.recovery.recover(project.id) == []
    assert services.academic.get_current_answers(project.id) is not None


def test_academic_recovery_missing_accepted_output_is_pending_retry(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)
    original = services.store.write_bytes

    def fail_accepted(root: str, path: str, content: bytes, **kwargs):  # type: ignore[no-untyped-def]
        if "/accepted/" in path:
            raise ArtifactError("INJECTED_WRITE_FAILURE", "write failed", recoverable=True)
        return original(root, path, content, **kwargs)

    monkeypatch.setattr(services.store, "write_bytes", fail_accepted)
    with pytest.raises(ArtifactError):
        services.academic.import_answers(project.id, answers_bytes())
    _running_answers(services, project.id)
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.PENDING_RETRY]
    assert services.recovery.recover(project.id) == []


def test_same_input_resumes_pending_retry_without_creating_another_document(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)
    original = services.store.write_bytes

    def fail_once(root: str, path: str, content: bytes, **kwargs):  # type: ignore[no-untyped-def]
        if "/accepted/" in path:
            raise ArtifactError("INJECTED_WRITE_FAILURE", "write failed", recoverable=True)
        return original(root, path, content, **kwargs)

    monkeypatch.setattr(services.store, "write_bytes", fail_once)
    raw = answers_bytes()
    with pytest.raises(ArtifactError):
        services.academic.import_answers(project.id, raw)
    monkeypatch.setattr(services.store, "write_bytes", original)

    accepted = services.academic.import_answers(project.id, raw)
    runs = [item for item in services.projects.runs(project.id) if item.stage_id == ANSWERS_STAGE]
    assert accepted.accepted_version == 1
    assert len(runs) == 1
    assert runs[0].status is RunStatus.DONE
    assert runs[0].attempt == 2


def test_academic_recovery_blocks_divergent_final_output(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)

    def crash_before_database(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash after promotion")

    monkeypatch.setattr(services.academic, "_finish_accepted", crash_before_database)
    with pytest.raises(IntegrityError):
        services.academic.import_answers(project.id, answers_bytes())
    run = _running_answers(services, project.id)
    accepted = services.store.resolve(
        project.artifact_root,
        "academic/consolidated-answers/accepted/from-raw-v0001.json",
    )
    accepted.write_bytes(b"corrupt")
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.BLOCKED]
    assert services.projects.runs(project.id)[-1].id == run.id
    assert services.projects.runs(project.id)[-1].status is RunStatus.BLOCKED
    with services.database.connection() as connection:
        document = services.academic.document  # keep service alive after restart boundary
        count = connection.execute(
            "SELECT count(*) FROM academic_documents WHERE project_id = ? "
            "AND document_kind = ? AND acceptance_status = 'accepted'",
            (project.id, DocumentKind.CONSOLIDATED_ANSWERS.value),
        ).fetchone()[0]
    assert document is not None
    assert count == 0


def test_recovery_reconciles_interrupted_questionnaire_confirmation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = services.projects.create(project_config_path)
    reviewed = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root, paraphrase=True)
    )

    def crash_after_promotion(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("confirmation checkpoint interrupted")

    monkeypatch.setattr(services.academic, "_finalize_confirmation", crash_after_promotion)
    with pytest.raises(IntegrityError):
        services.academic.confirm_questionnaire(project.id, reviewed.raw_version)
    _force_latest_running(services, project.id, QUESTIONNAIRE_STAGE, confirmation=True)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.DONE]
    confirmed, _ = services.academic.document(project.id, DocumentKind.QUESTIONNAIRE)
    assert confirmed.accepted_version == 1
    assert services.recovery.recover(project.id) == []


def test_recovery_deterministically_finishes_rejected_raw_input(
    services: ServiceBundle,
    project_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = services.projects.create(project_config_path)

    def crash_before_rejection_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("rejection checkpoint interrupted")

    monkeypatch.setattr(services.academic, "_finish_rejected", crash_before_rejection_checkpoint)
    with pytest.raises(OSError):
        services.academic.import_questionnaire(project.id, b"1. A\n2. B\n3. C\n4. D")
    _force_latest_running(services, project.id, QUESTIONNAIRE_STAGE)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.BLOCKED]
    assert services.academic.status(project.id)["questionnaire"]["status"] == "rejected"  # type: ignore[index]


def test_recovery_without_academic_operation_metadata_is_pending_retry(
    services: ServiceBundle,
    project_config_path: Path,
) -> None:
    project = services.projects.create(project_config_path)
    pending = services.academic.state_machine.new_run(
        project_id=project.id,
        stage_id=ANSWERS_STAGE,
        unit_id=ANSWERS_STAGE,
        input_hash="1" * 64,
        max_attempts=3,
    )
    running = services.academic.state_machine.transition(pending, RunStatus.RUNNING)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        repository = StageRunRepository(connection)
        repository.add(pending)
        repository.update(running, expected_status=pending.status)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.PENDING_RETRY]


def test_recovery_preserves_review_required_questionnaire_after_checkpoint_crash(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = services.projects.create(project_config_path)

    def crash_review_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("review-required checkpoint interrupted")

    monkeypatch.setattr(services.academic, "_finish_review_required", crash_review_checkpoint)
    with pytest.raises(IntegrityError):
        services.academic.import_questionnaire(
            project.id, questionnaire_bytes(repository_root, paraphrase=True)
        )
    _force_latest_running(services, project.id, QUESTIONNAIRE_STAGE)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.DONE]
    assert services.academic.status(project.id)["questionnaire"]["status"] == "review_required"  # type: ignore[index]


def test_recovery_reconciles_promoted_academic_plan(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)
    services.academic.import_answers(project.id, answers_bytes())
    services.academic.authorize_plan(project.id)

    def crash_plan_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("plan checkpoint interrupted")

    monkeypatch.setattr(services.academic, "_finish_accepted", crash_plan_checkpoint)
    with pytest.raises(IntegrityError):
        services.academic.import_plan(project.id, "# Plano recuperável".encode())
    _force_latest_running(services, project.id, PLAN_STAGE)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.DONE]
    assert services.academic.get_current_compatible_academic_plan(project.id) is not None


def test_recovery_blocks_prompt_binding_drift(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _prepare(services, project_config_path, repository_root)

    def crash_answers_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("answers checkpoint interrupted")

    monkeypatch.setattr(services.academic, "_finish_accepted", crash_answers_checkpoint)
    with pytest.raises(IntegrityError):
        services.academic.import_answers(project.id, answers_bytes())
    run = _force_latest_running(services, project.id, ANSWERS_STAGE)
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE academic_documents SET prompt_sha256 = ? WHERE stage_run_id = ?",
            ("0" * 64, run.id),
        )

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.BLOCKED]
    assert "binding" in result[0].message


def test_confirmation_recovery_with_missing_output_becomes_pending_retry(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = services.projects.create(project_config_path)
    reviewed = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root, paraphrase=True)
    )
    original = services.store.write_bytes

    def fail_confirmation(root: str, path: str, content: bytes, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("accepted/from-raw-v0001.json"):
            raise ArtifactError(
                "INJECTED_CONFIRM_FAILURE", "confirmation write failed", recoverable=True
            )
        return original(root, path, content, **kwargs)

    monkeypatch.setattr(services.store, "write_bytes", fail_confirmation)
    with pytest.raises(ArtifactError):
        services.academic.confirm_questionnaire(project.id, reviewed.raw_version)
    _force_latest_running(services, project.id, QUESTIONNAIRE_STAGE, confirmation=True)

    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.PENDING_RETRY]
