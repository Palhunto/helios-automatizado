from __future__ import annotations

from pathlib import Path

from conftest import ServiceBundle
from test_writing_flow import _confirmed_context, _ready_project

from ebook_pipeline.writing.service import WritingService


def test_acknowledgement_is_bound_to_exact_context_after_newer_context_exists(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    first = services.writing.create_context(project_id, contract_id="synthetic_demo")
    second = services.writing.create_context(project_id, contract_id="synthetic_dag")
    assert second.id != first.id
    acknowledgement = services.writing.acknowledge_context_by_id(
        project_id, first.id, b"Acknowledgement for the first immutable context."
    )
    assert acknowledgement.context_id == first.id
    restarted = WritingService(
        services.config, services.database, services.store, services.academic
    )
    assert restarted.context_package_by_id(project_id, first.id)[0].id == first.id
    assert restarted.confirm_context_by_id(project_id, first.id).context_id == first.id


def test_submission_cannot_drift_to_newer_preparation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    first = services.writing.prepare_unit(project_id, "START")
    second = services.writing.prepare_unit(project_id, "START", reprocess=True)
    assert second.id != first.id
    request_item, request = services.writing.unit_request_by_id(project_id, first.id)
    assert request_item.id == first.id and request
    raw = b"Texto vinculado exatamente a preparacao A, mesmo depois de B existir."
    submission = services.writing.import_unit_by_preparation_id(project_id, first.id, raw)
    assert submission.preparation_id == first.id
    assert submission.preparation_id != second.id


def test_raw_versions_preserve_history_and_are_idempotent_across_preparations(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    first_context = services.writing.create_context(project_id, contract_id="synthetic_demo")
    first_ack = services.writing.acknowledge_context_by_id(
        project_id, first_context.id, b"Primeiro contexto confirmado para o teste."
    )
    services.writing.confirm_context_by_id(
        project_id, first_context.id, first_ack.raw_version
    )
    first_preparation = services.writing.prepare_unit_for_context(
        project_id, first_context.id, "START"
    )
    first_raw = b"Bytes A preservados como primeira submissao valida desta unidade."
    first = services.writing.import_unit_by_preparation_id(
        project_id, first_preparation.id, first_raw
    )

    second_preparation = services.writing.prepare_unit_for_context(
        project_id, first_context.id, "START", reprocess=True
    )
    second_raw = b"Bytes B preservados como nova submissao valida da mesma unidade."
    second = services.writing.import_unit_by_preparation_id(
        project_id, second_preparation.id, second_raw
    )

    project = services.projects.get(project_id)
    root = services.store.project_root(project.artifact_root)
    assert first.raw_version == 1
    assert second.raw_version == 2
    assert (root / "text/raw/START/v0001.txt").read_bytes() == first_raw
    assert (root / "text/raw/START/v0002.txt").read_bytes() == second_raw

    with services.database.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM text_unit_submissions "
            "WHERE project_id = ? AND unit_id = 'START'",
            (project_id,),
        ).fetchone()[0]
    assert count == 2


def test_reimporting_same_preparation_response_reuses_raw_submission(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    preparation = services.writing.prepare_unit(project_id, "START")
    raw = b"Mesma resposta integral deve manter uma unica submissao raw persistida."

    first = services.writing.import_unit_by_preparation_id(project_id, preparation.id, raw)
    repeated = services.writing.import_unit_by_preparation_id(project_id, preparation.id, raw)

    assert repeated == first
    assert first.raw_version == 1
    with services.database.connection() as connection:
        submission_count = connection.execute(
            "SELECT count(*) FROM text_unit_submissions WHERE preparation_id = ?",
            (preparation.id,),
        ).fetchone()[0]
        artifact_count = connection.execute(
            "SELECT count(*) FROM artifacts WHERE project_id = ? "
            "AND relative_path LIKE 'text/raw/START/%'",
            (project_id,),
        ).fetchone()[0]
    assert submission_count == 1
    assert artifact_count == 1
