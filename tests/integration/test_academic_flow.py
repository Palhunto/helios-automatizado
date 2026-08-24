from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.academic.models import AcceptanceStatus, DocumentKind
from ebook_pipeline.academic.parsers import ANSWER_HEADER, ANSWER_TITLES
from ebook_pipeline.academic.repositories import AcademicDocumentRepository
from ebook_pipeline.academic.validators import canonical_questions
from ebook_pipeline.core.errors import (
    AcademicValidationError,
    ConflictError,
    IntegrityError,
    NotFoundError,
)
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.prompts import PromptRegistry


def questionnaire_bytes(
    repository_root: Path, *, paraphrase: bool = False, numbered: bool = True
) -> bytes:
    prompt = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    questions = list(canonical_questions(prompt))
    if paraphrase:
        questions[1] = "Qual é o perfil esperado dos alunos desta disciplina?"
    if numbered:
        return ("\n".join(f"{index}. {text}" for index, text in enumerate(questions, 1))).encode()
    return "\n".join(questions).encode()


def answers_bytes(*, conflict: bool = False, pending: bool = False, suffix: str = "") -> bytes:
    lines = [ANSWER_HEADER]
    for number, title in enumerate(ANSWER_TITLES, start=1):
        lines.extend((f"{number}. {title}", f"[CONFIRMADO] Resposta {number}{suffix}."))
    if conflict:
        lines.insert(5, "[CONFLITO] Informação incompatível.")
    if pending:
        lines.insert(5, "[PENDENTE] Informação ainda não definida.")
    return "\n".join(lines).encode()


def _create(services: ServiceBundle, project_config_path: Path):  # type: ignore[no-untyped-def]
    return services.projects.create(project_config_path)


def test_questionnaire_import_is_idempotent_and_preserves_observed_text(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> None:
    project = _create(services, project_config_path)
    raw = (
        repository_root / "tests" / "fixtures" / "m1_real_unnumbered_questionnaire.txt"
    ).read_bytes()
    first = services.academic.import_questionnaire(project.id, raw)
    second = services.academic.import_questionnaire(project.id, raw)
    assert first == second
    assert first.acceptance_status is AcceptanceStatus.ACCEPTED
    document, content = services.academic.document(project.id, DocumentKind.QUESTIONNAIRE)
    payload = json.loads(content)
    assert document.id == first.id
    assert (
        payload["questions"][0]["observed_text"]
        == canonical_questions(
            PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
                "omega_academic_planning", 1
            )
        )[0]
    )
    _, preserved_raw = services.academic.document(
        project.id, DocumentKind.QUESTIONNAIRE, version=1, raw=True
    )
    assert preserved_raw == raw
    runs = [
        run
        for run in services.projects.runs(project.id)
        if run.stage_id == "academic_questionnaire"
    ]
    assert len(runs) == 1


def test_questionnaire_review_confirmation_never_replaces_observed_paraphrase(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> None:
    project = _create(services, project_config_path)
    observed = "Qual é o perfil esperado dos alunos desta disciplina?"
    reviewed = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root, paraphrase=True, numbered=False)
    )
    assert reviewed.acceptance_status is AcceptanceStatus.REVIEW_REQUIRED
    assert reviewed.accepted_artifact_id is None
    confirmed = services.academic.confirm_questionnaire(project.id, reviewed.raw_version)
    repeated = services.academic.confirm_questionnaire(project.id, reviewed.raw_version)
    assert repeated == confirmed
    _, content = services.academic.document(project.id, DocumentKind.QUESTIONNAIRE)
    payload = json.loads(content)
    question = payload["questions"][1]
    assert question["observed_text"] == observed
    assert question["observed_text"] != question["canonical_text"]
    assert question["canonical_question_number"] == 2
    assert question["wording_status"] == "review_confirmed"


def test_invalid_questionnaire_preserves_raw_and_blocks_only_that_run(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = _create(services, project_config_path)
    raw = b"1. A\n2. B\n3. C\n4. D"
    with pytest.raises(AcademicValidationError):
        services.academic.import_questionnaire(project.id, raw)
    documents = services.academic.status(project.id)
    assert documents["questionnaire"]["status"] == "rejected"  # type: ignore[index]
    academic_run = [
        run
        for run in services.projects.runs(project.id)
        if run.stage_id == "academic_questionnaire"
    ][0]
    assert academic_run.status is RunStatus.BLOCKED
    raw_path = services.store.resolve(project.artifact_root, "academic/questionnaire/raw/v0001.txt")
    assert raw_path.read_bytes() == raw


def test_full_flow_authorization_restart_and_stale_plan(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    services.academic.import_questionnaire(project.id, questionnaire_bytes(repository_root))
    answers_v1 = services.academic.import_answers(project.id, answers_bytes())
    with pytest.raises(ConflictError) as unauthorized:
        services.academic.import_plan(project.id, b"# Plano V1")
    assert unauthorized.value.code == "ACADEMIC_PLAN_NOT_AUTHORIZED"
    authorization = services.academic.authorize_plan(project.id)
    assert authorization.answers_document_id == answers_v1.id
    plan_v1 = services.academic.import_plan(project.id, b"# Plano V1")
    assert services.academic.get_current_compatible_academic_plan(project.id) is not None

    answers_v2 = services.academic.import_answers(project.id, answers_bytes(suffix=" atualizada"))
    assert answers_v2.raw_version == 2
    assert services.academic.get_current_compatible_academic_plan(project.id) is None
    with services.database.connection() as connection:
        plans = [
            item
            for item in AcademicDocumentRepository(connection).list_for_project(project.id)
            if item.document_kind is DocumentKind.ACADEMIC_PLAN
        ]
    assert plans == [plan_v1]
    assert services.academic.status(project.id)["academic_plan"]["current"] is False  # type: ignore[index]


def test_conflict_blocks_authorization_but_pending_does_not(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    services.academic.import_questionnaire(project.id, questionnaire_bytes(repository_root))
    services.academic.import_answers(project.id, answers_bytes(conflict=True))
    with pytest.raises(ConflictError) as blocked:
        services.academic.authorize_plan(project.id)
    assert blocked.value.code == "ACADEMIC_CONFLICT_BLOCKS_AUTHORIZATION"
    services.academic.import_answers(project.id, answers_bytes(pending=True, suffix=" corrigida"))
    assert services.academic.authorize_plan(project.id).conflict_count == 0


def test_academic_preconditions_and_missing_views_are_explicit(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    status = services.academic.status(project.id)
    assert status["questionnaire"] == {"status": "missing"}
    assert services.academic.get_current_answers(project.id) is None
    assert services.academic.get_current_compatible_academic_plan(project.id) is None

    with pytest.raises(ConflictError) as answers_missing:
        services.academic.import_answers(project.id, answers_bytes())
    assert answers_missing.value.code == "QUESTIONNAIRE_NOT_ACCEPTED"
    with pytest.raises(ConflictError) as authorization_missing:
        services.academic.authorize_plan(project.id)
    assert authorization_missing.value.code == "ANSWERS_NOT_ACCEPTED"
    with pytest.raises(ConflictError) as plan_missing:
        services.academic.import_plan(project.id, b"# Plano")
    assert plan_missing.value.code == "ANSWERS_NOT_ACCEPTED"
    with pytest.raises(NotFoundError):
        services.academic.confirm_questionnaire(project.id, 99)
    with pytest.raises(NotFoundError):
        services.academic.document(project.id, DocumentKind.QUESTIONNAIRE)

    accepted = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root)
    )
    assert services.academic.confirm_questionnaire(project.id, accepted.raw_version) == accepted


def test_academic_versions_are_idempotent_and_raw_is_independently_readable(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    raw_questionnaire = questionnaire_bytes(repository_root)
    questionnaire = services.academic.import_questionnaire(project.id, raw_questionnaire)
    raw_document, raw_content = services.academic.document(
        project.id, DocumentKind.QUESTIONNAIRE, version=1, raw=True
    )
    accepted_document, _ = services.academic.document(
        project.id, DocumentKind.QUESTIONNAIRE, version=1
    )
    assert raw_document == accepted_document == questionnaire
    assert raw_content == raw_questionnaire

    answer_raw = answers_bytes()
    answers = services.academic.import_answers(project.id, answer_raw)
    assert services.academic.import_answers(project.id, answer_raw) == answers
    authorization = services.academic.authorize_plan(project.id)
    assert services.academic.authorize_plan(project.id) == authorization
    plan_bytes = "# Plano estável".encode()
    plan = services.academic.import_plan(project.id, plan_bytes)
    assert services.academic.import_plan(project.id, plan_bytes) == plan
    assert services.academic.document(project.id, DocumentKind.ACADEMIC_PLAN, version=1)[0] == plan


def test_rejected_input_is_not_silently_retried_and_review_binding_is_verified(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    invalid = b"1. A\n2. B\n3. C\n4. D"
    with pytest.raises(AcademicValidationError):
        services.academic.import_questionnaire(project.id, invalid)
    with pytest.raises(AcademicValidationError) as repeated:
        services.academic.import_questionnaire(project.id, invalid)
    assert repeated.value.code == "ACADEMIC_INPUT_PREVIOUSLY_REJECTED"
    with pytest.raises(ConflictError) as not_reviewable:
        services.academic.confirm_questionnaire(project.id, 1)
    assert not_reviewable.value.code == "QUESTIONNAIRE_NOT_REVIEWABLE"

    reviewed = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root, paraphrase=True)
    )
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE academic_documents SET prompt_sha256 = ? WHERE id = ?",
            ("0" * 64, reviewed.id),
        )
    with pytest.raises(IntegrityError) as mismatch:
        services.academic.confirm_questionnaire(project.id, reviewed.raw_version)
    assert mismatch.value.code == "PROMPT_HASH_MISMATCH"


def test_academic_validate_reports_corrupt_artifact_without_repair(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project = _create(services, project_config_path)
    document = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root)
    )
    assert document.accepted_artifact_id is not None
    accepted = services.store.resolve(
        project.artifact_root, "academic/questionnaire/accepted/from-raw-v0001.json"
    )
    accepted.write_bytes(b"corrupt")
    issues = services.academic.validate(project.id)
    assert any(issue.code == "ACADEMIC_ARTIFACT_HASH_MISMATCH" for issue in issues)
    with pytest.raises(IntegrityError) as corrupted:
        services.academic.document(project.id, DocumentKind.QUESTIONNAIRE)
    assert corrupted.value.code == "ACADEMIC_ARTIFACT_HASH_MISMATCH"
