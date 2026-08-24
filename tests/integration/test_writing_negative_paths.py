from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_writing_flow import _confirmed_context, _ready_project

from ebook_pipeline.academic.models import CurrentWritingInputs
from ebook_pipeline.core.errors import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    WritingValidationError,
)
from ebook_pipeline.writing.models import SubmissionDisposition
from ebook_pipeline.writing.service import WritingService


def _crashing_service(services: ServiceBundle, target: str) -> WritingService:
    def fault_hook(checkpoint: str) -> None:
        if checkpoint == target:
            raise RuntimeError(target)

    return WritingService(
        services.config,
        services.database,
        services.store,
        services.academic,
        fault_hook=fault_hook,
    )


def test_context_preconditions_lookup_validation_and_idempotence(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = services.projects.create(project_config_path).id
    with pytest.raises(ConflictError):
        services.writing.create_context(project_id, contract_id="synthetic_demo")
    with pytest.raises(NotFoundError):
        services.writing.contexts.latest(project_id)
    with pytest.raises(NotFoundError):
        services.writing.contexts.get(project_id, 99)
    with pytest.raises(ConflictError):
        services.writing.acknowledge_context(project_id, b"valid but premature")
    with pytest.raises(ConflictError):
        services.writing.confirm_context(project_id)
    with pytest.raises(WritingValidationError):
        services.writing.acknowledge_context(project_id, b"\xff")
    with pytest.raises(WritingValidationError):
        services.writing.acknowledge_context(project_id, b" \r\n")

    project_id = _ready_project(services, project_config_path, repository_root)
    context = services.writing.create_context(project_id, contract_id="synthetic_demo")
    assert services.writing.create_context(project_id, contract_id="synthetic_demo") == context
    assert services.writing.contexts.get(project_id, context.version) == context
    with pytest.raises(NotFoundError):
        services.writing.contexts.get(project_id, 99)
    with pytest.raises(NotFoundError):
        services.writing.confirm_context(project_id)

    acknowledgement = services.writing.acknowledge_context(project_id, b"Contexto compreendido.")
    assert (
        services.writing.acknowledge_context(project_id, b"Contexto compreendido.")
        == acknowledgement
    )
    confirmed = services.writing.confirm_context(project_id, acknowledgement.raw_version)
    assert services.writing.confirm_context(project_id, acknowledgement.raw_version) == confirmed
    with services.database.connection() as connection:
        assert services.writing.contexts.is_confirmed(connection, context.id)
    payload = services.writing.contexts.payload(context)
    assert payload["confirmed"] is True and payload["current"] is True

    with services.database.connection() as connection:
        connection.execute(
            "UPDATE writing_contexts SET package_artifact_id = NULL WHERE id = ?",
            (context.id,),
        )
        connection.commit()
    with pytest.raises(IntegrityError):
        services.writing.context_package(project_id)

    altered = replace(context, contract_sha256="0" * 64)
    with pytest.raises(IntegrityError):
        services.writing.contexts.resolve_contract(altered)


def test_context_creation_rechecks_m1_snapshot_under_write_lock(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    original = services.academic.get_current_writing_inputs
    calls = 0

    def changing_inputs(selected_project_id: str) -> CurrentWritingInputs | None:
        nonlocal calls
        calls += 1
        return original(selected_project_id) if calls == 1 else None

    monkeypatch.setattr(services.academic, "get_current_writing_inputs", changing_inputs)
    with pytest.raises(ConflictError) as captured:
        services.writing.create_context(project_id, contract_id="synthetic_demo")
    assert captured.value.code == "WRITING_ACADEMIC_INPUTS_CHANGED"
    with services.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM writing_contexts").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM stage_runs WHERE stage_id = 'writing_context_load'"
            ).fetchone()[0]
            == 0
        )


def test_preparation_guards_idempotence_and_interrupted_identity(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    services.writing.create_context(project_id, contract_id="synthetic_demo")
    with pytest.raises(ConflictError):
        services.writing.prepare_unit(project_id, "START")
    with pytest.raises(NotFoundError):
        services.writing.preparations.latest(project_id, "START")
    with pytest.raises(NotFoundError):
        services.writing.unit_request(project_id, "START")

    acknowledgement = services.writing.acknowledge_context(project_id, b"Compreendido.")
    services.writing.confirm_context(project_id, acknowledgement.raw_version)
    with pytest.raises(ConflictError):
        services.writing.prepare_unit(project_id, "BODY")

    preparation = services.writing.prepare_unit(project_id, "START")
    assert services.writing.prepare_unit(project_id, "START") == preparation
    assert services.writing.preparations.latest(project_id, "START") == preparation
    assert services.writing.unit_request(project_id, "START")[1]

    crashing = _crashing_service(services, "preparation.request")
    with pytest.raises(RuntimeError):
        crashing.prepare_unit(project_id, "START", reprocess=True)
    with pytest.raises(IntegrityError) as captured:
        services.writing.prepare_unit(project_id, "START")
    assert captured.value.code == "WRITING_RECOVERY_REQUIRED"


def test_submission_guards_raw_access_and_interrupted_identity(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    with pytest.raises(ConflictError):
        services.writing.import_unit(project_id, "START", b"Texto valido e suficiente.")
    with pytest.raises(NotFoundError):
        services.writing.unit(project_id, "START")

    preparation = services.writing.prepare_unit(project_id, "START")
    with services.database.connection() as connection:
        finished_at = connection.execute(
            "SELECT finished_at FROM stage_runs WHERE id = ?",
            (preparation.stage_run_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE id = ?",
            (preparation.stage_run_id,),
        )
        connection.commit()
    with pytest.raises(IntegrityError):
        services.writing.import_unit(project_id, "START", b"Texto valido e suficiente.")
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'done', finished_at = ? WHERE id = ?",
            (finished_at, preparation.stage_run_id),
        )
        connection.commit()

    raw = b"Conteudo suficientemente desenvolvido para ser aceito de forma objetiva."
    accepted = services.writing.import_unit(project_id, "START", raw)
    assert services.writing.import_unit(project_id, "START", raw) == accepted
    assert services.writing.unit(project_id, "START", raw=True)[1] == raw
    with pytest.raises(NotFoundError):
        services.writing.unit(project_id, "START", version=99, raw=True)

    services.writing.prepare_unit(project_id, "START", reprocess=True)
    rejected = services.writing.import_unit(project_id, "START", b"curto")
    assert rejected.disposition is SubmissionDisposition.REJECTED
    with pytest.raises(NotFoundError):
        services.writing.unit(project_id, "START", version=rejected.raw_version, raw=False)

    services.writing.prepare_unit(project_id, "START", reprocess=True)
    crashing = _crashing_service(services, "submission.raw")
    interrupted_raw = b"Outro conteudo suficientemente longo para importacao."
    with pytest.raises(RuntimeError):
        crashing.import_unit(project_id, "START", interrupted_raw)
    with pytest.raises(IntegrityError) as captured:
        services.writing.import_unit(project_id, "START", interrupted_raw)
    assert captured.value.code == "WRITING_RECOVERY_REQUIRED"


def test_consolidation_requires_complete_set_and_show_requires_artifact(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    with pytest.raises(ConflictError) as incomplete:
        services.writing.consolidate(project_id)
    assert incomplete.value.code == "TEXT_PRODUCTION_SET_INCOMPLETE"
    with pytest.raises(NotFoundError):
        services.writing.consolidated(project_id)
    with pytest.raises(NotFoundError):
        services.writing.consolidated(project_id, version=99)
