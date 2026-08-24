from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_writing_flow import _accept, _confirmed_context

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.writing.models import SubmissionDisposition
from ebook_pipeline.writing.repositories import (
    AcknowledgementRepository,
    CitationRepository,
    ConsolidationRepository,
    PreparationRepository,
    SubmissionRepository,
    WritingArtifactRepository,
    WritingContextRepository,
)


def test_writing_repository_queries_idempotent_updates_and_conflicts(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    start = _accept(
        services,
        project_id,
        "START",
        "Porter (1980) fundamenta um desenvolvimento textual válido.",
    )
    _accept(
        services,
        project_id,
        "BODY",
        "Desenvolvimento central consistente e suficientemente detalhado para validação.",
    )
    _accept(services, project_id, "END", "Síntese final válida e preservada.")
    consolidation = services.writing.consolidate(project_id)

    with services.database.connection() as connection:
        contexts = WritingContextRepository(connection)
        context = contexts.latest(project_id)
        assert context is not None
        assert contexts.get(context.id) == context
        assert contexts.get_by_run(context.stage_run_id) == context
        assert contexts.get_by_run("missing") is None
        assert contexts.by_version(project_id, context.version) == context
        assert contexts.by_version(project_id, 999) is None
        assert contexts.by_input(project_id, context.input_hash) == context
        assert contexts.list_for_project(project_id) == [context]
        assert context.package_artifact_id is not None
        assert context.manifest_artifact_id is not None
        assert (
            contexts.set_artifacts(
                context, context.package_artifact_id, context.manifest_artifact_id
            )
            == context
        )
        with pytest.raises(ConflictError):
            contexts.set_artifacts(
                context, context.manifest_artifact_id, context.package_artifact_id
            )
        with pytest.raises(ConflictError):
            contexts.add(context)
        with pytest.raises(NotFoundError):
            contexts.get("missing")

        acknowledgements = AcknowledgementRepository(connection)
        acknowledgement = acknowledgements.latest(context.id)
        assert acknowledgement is not None
        assert acknowledgements.get(acknowledgement.id) == acknowledgement
        assert acknowledgements.get_by_run(acknowledgement.stage_run_id) == acknowledgement
        assert acknowledgements.get_by_run("missing") is None
        assert acknowledgements.by_raw_version(context.id, 999) is None
        assert acknowledgements.by_hash(context.id, acknowledgement.raw_sha256) == acknowledgement
        assert acknowledgements.confirmed(context.id) == acknowledgement
        assert acknowledgement.raw_artifact_id is not None
        assert (
            acknowledgements.set_artifact(acknowledgement, acknowledgement.raw_artifact_id)
            == acknowledgement
        )
        assert acknowledgements.confirm(acknowledgement, "ignored") == acknowledgement
        with pytest.raises(NotFoundError):
            acknowledgements.get("missing")
        with pytest.raises(ConflictError):
            acknowledgements.add(acknowledgement)

        preparations = PreparationRepository(connection)
        preparation = preparations.latest(context.id, "START")
        assert preparation is not None
        assert preparations.get(preparation.id) == preparation
        assert preparations.get_by_run(preparation.stage_run_id) == preparation
        assert preparations.get_by_run("missing") is None
        assert preparations.by_input(context.id, "START", preparation.input_hash) == preparation
        assert preparations.list_for_context(context.id)
        assert preparations.dependencies(preparation.id) == ()
        assert preparation.request_artifact_id is not None
        assert preparation.manifest_artifact_id is not None
        assert (
            preparations.set_artifacts(
                preparation,
                preparation.request_artifact_id,
                preparation.manifest_artifact_id,
            )
            == preparation
        )
        with pytest.raises(ConflictError):
            preparations.set_artifacts(
                preparation,
                preparation.manifest_artifact_id,
                preparation.request_artifact_id,
            )
        with pytest.raises(ConflictError):
            preparations.add(preparation)
        with pytest.raises(NotFoundError):
            preparations.get("missing")

        body_preparation = preparations.latest(context.id, "BODY")
        assert body_preparation is not None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE text_unit_submissions SET preparation_id = ? WHERE id = ?",
                (body_preparation.id, start.id),
            )

        submissions = SubmissionRepository(connection)
        assert submissions.get(start.id) == start
        assert submissions.get_by_run(start.stage_run_id) == start
        assert submissions.get_by_run("missing") is None
        assert submissions.by_preparation_hash(start.preparation_id, start.raw_sha256) == start
        assert submissions.by_raw_version(context.id, "START", 999) is None
        assert submissions.by_accepted_version(context.id, "START", 999) is None
        assert submissions.latest(context.id, "START") == start
        assert start.raw_artifact_id is not None
        assert start.validation_report_artifact_id is not None
        assert start.accepted_artifact_id is not None
        assert (
            submissions.materialize(
                start,
                disposition=SubmissionDisposition.ACCEPTED,
                raw_artifact_id=start.raw_artifact_id,
                report_artifact_id=start.validation_report_artifact_id,
                accepted_artifact_id=start.accepted_artifact_id,
                citation_ledger_sha256=start.citation_ledger_sha256,
                citation_ledger_artifact_id=start.citation_ledger_artifact_id,
                accepted_version=start.accepted_version,
                accepted_at=start.accepted_at,
            )
            == start
        )
        with pytest.raises(ConflictError):
            submissions.materialize(
                start,
                disposition=SubmissionDisposition.REJECTED,
                raw_artifact_id=start.raw_artifact_id,
                report_artifact_id=start.validation_report_artifact_id,
                accepted_artifact_id=None,
                citation_ledger_sha256=None,
                citation_ledger_artifact_id=None,
                accepted_version=None,
                accepted_at=None,
            )
        with pytest.raises(ConflictError):
            submissions.add(start)
        with pytest.raises(NotFoundError):
            submissions.get("missing")
        assert CitationRepository(connection).for_submission(start.id)

        consolidations = ConsolidationRepository(connection)
        assert consolidations.get(consolidation.id) == consolidation
        assert consolidations.get_by_run(consolidation.stage_run_id) == consolidation
        assert consolidations.get_by_run("missing") is None
        assert (
            consolidations.by_set_hash(context.id, consolidation.production_set_hash)
            == consolidation
        )
        assert consolidation.text_artifact_id is not None
        assert consolidation.manifest_artifact_id is not None
        assert (
            consolidations.set_artifacts(
                consolidation,
                consolidation.text_artifact_id,
                consolidation.manifest_artifact_id,
            )
            == consolidation
        )
        with pytest.raises(ConflictError):
            consolidations.set_artifacts(
                consolidation,
                consolidation.manifest_artifact_id,
                consolidation.text_artifact_id,
            )
        assert len(consolidations.members(consolidation.id)) == 3
        with pytest.raises(ConflictError):
            consolidations.add(consolidation)
        with pytest.raises(NotFoundError):
            consolidations.get("missing")
        with pytest.raises(NotFoundError):
            WritingArtifactRepository(connection).get("missing")
