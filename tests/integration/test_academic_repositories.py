from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_academic_flow import answers_bytes, questionnaire_bytes

from ebook_pipeline.academic.models import AcceptanceStatus, DocumentKind
from ebook_pipeline.academic.repositories import (
    AcademicArtifactRepository,
    AcademicDocumentRepository,
    AuthorizationRepository,
    ReviewDecisionRepository,
)
from ebook_pipeline.core.errors import ConflictError, NotFoundError


def test_academic_repository_queries_and_idempotent_updates(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> None:
    project = services.projects.create(project_config_path)
    questionnaire = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root)
    )
    answers = services.academic.import_answers(project.id, answers_bytes())
    authorization = services.academic.authorize_plan(project.id)

    with services.database.connection() as connection:
        documents = AcademicDocumentRepository(connection)
        artifacts = AcademicArtifactRepository(connection)
        authorizations = AuthorizationRepository(connection)

        assert documents.get_by_run("missing") is None
        assert documents.by_raw_version(project.id, DocumentKind.QUESTIONNAIRE, 99) is None
        assert documents.by_accepted_version(project.id, DocumentKind.QUESTIONNAIRE, 99) is None
        assert (
            documents.by_accepted_version(project.id, DocumentKind.QUESTIONNAIRE, 1)
            == questionnaire
        )
        with pytest.raises(NotFoundError):
            documents.get("missing")

        assert questionnaire.raw_artifact_id is not None
        assert questionnaire.accepted_artifact_id is not None
        assert (
            documents.set_raw_artifact(questionnaire, questionnaire.raw_artifact_id)
            == questionnaire
        )
        with pytest.raises(ConflictError):
            documents.set_raw_artifact(questionnaire, questionnaire.accepted_artifact_id)
        with pytest.raises(ConflictError):
            documents.set_disposition(questionnaire, AcceptanceStatus.REJECTED)
        with pytest.raises(ConflictError):
            documents.set_disposition(questionnaire, AcceptanceStatus.ACCEPTED)

        assert (
            documents.accept(questionnaire, questionnaire.accepted_artifact_id, 1) == questionnaire
        )
        with pytest.raises(ConflictError):
            documents.accept(questionnaire, answers.accepted_artifact_id or "missing", 2)

        assert authorizations.get_for_answers(answers.id) == authorization
        assert authorizations.add(project.id, answers.id) == authorization
        with pytest.raises(NotFoundError):
            artifacts.get("missing")
        assert artifacts.for_run(answers.stage_run_id)


def test_review_decision_is_persisted_and_document_uniqueness_is_enforced(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> None:
    project = services.projects.create(project_config_path)
    reviewed = services.academic.import_questionnaire(
        project.id, questionnaire_bytes(repository_root, paraphrase=True)
    )
    confirmed = services.academic.confirm_questionnaire(project.id, reviewed.raw_version)

    with services.database.connection() as connection:
        documents = AcademicDocumentRepository(connection)
        decisions = ReviewDecisionRepository(connection)
        decision = decisions.get_for_document(confirmed.id)
        assert decision is not None
        assert decision.decision == "confirm_observed_questionnaire"
        assert decisions.add(project.id, confirmed.id, decision.stage_run_id) == decision
        with pytest.raises(ConflictError):
            documents.add(confirmed)
