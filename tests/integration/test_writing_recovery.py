from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_writing_flow import _accept, _answers, _confirmed_context, _ready_project

from ebook_pipeline.cli import main
from ebook_pipeline.core.errors import AcademicValidationError, ConflictError, IntegrityError
from ebook_pipeline.core.hashing import canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.writing.consolidation import CONSOLIDATION_STAGE
from ebook_pipeline.writing.context import CONTEXT_STAGE
from ebook_pipeline.writing.preparation import TEXT_STAGE
from ebook_pipeline.writing.repositories import PreparationRepository
from ebook_pipeline.writing.service import WritingService


def _crashing_service(services: ServiceBundle, target: str) -> WritingService:
    def fault_hook(checkpoint: str) -> None:
        if checkpoint == target:
            raise RuntimeError(f"simulated crash at {target}")

    return WritingService(
        services.config,
        services.database,
        services.store,
        services.academic,
        fault_hook=fault_hook,
    )


def _cli_base(tmp_path: Path, repository_root: Path) -> list[str]:
    return [
        "--data-dir",
        str(tmp_path / "data"),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--pipeline-config",
        str(repository_root / "config" / "pipeline.yaml"),
        "--prompt-registry",
        str(repository_root / "prompts" / "registry.yaml"),
        "--writing-contract-registry",
        str(repository_root / "writing_contracts" / "registry.yaml"),
    ]


def test_recovery_reconciles_context_creation_and_detects_corruption(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    crashing = _crashing_service(services, "context.artifacts")
    with pytest.raises(RuntimeError):
        crashing.create_context(project_id, contract_id="synthetic_demo")
    result = services.recovery.recover(project_id)
    assert [item.status for item in result] == [RunStatus.DONE]
    assert services.writing.context_package(project_id)[1]
    assert services.recovery.recover(project_id) == []

    with pytest.raises(AcademicValidationError):
        services.academic.import_answers(project_id, b"invalid")
    # Historical context remains immutable even if a later academic attempt is rejected.
    assert services.writing.context_package(project_id)[1]


def test_recovery_reconciles_acknowledgement_confirmation_and_preparation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    services.writing.create_context(project_id, contract_id="synthetic_demo")
    crashing_ack = _crashing_service(services, "acknowledgement.raw")
    with pytest.raises(RuntimeError):
        crashing_ack.acknowledge_context(project_id, b"Contexto avaliado.")
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE

    acknowledgement = services.writing.acknowledge_context(project_id, b"Contexto avaliado.")
    crashing_confirm = _crashing_service(services, "acknowledgement.confirmed")
    with pytest.raises(RuntimeError):
        crashing_confirm.confirm_context(project_id, acknowledgement.raw_version)
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE

    crashing_prepare = _crashing_service(services, "preparation.manifest")
    with pytest.raises(RuntimeError):
        crashing_prepare.prepare_unit(project_id, "START")
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE
    assert services.writing.unit_request(project_id, "START")[1]


def test_recovery_reallocates_strict_legacy_preparation_destination_conflict(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    first_context = services.writing.context(project_id)
    first_start = _accept(
        services,
        project_id,
        "START",
        "Fundamento inicial suficientemente desenvolvido para o primeiro contexto.",
    )
    first_body = services.writing.prepare_unit_for_context(
        project_id, first_context.id, "BODY"
    )
    project = services.projects.get(project_id)
    root = services.store.project_root(project.artifact_root)
    historical_request_path = root / "text/preparations/BODY/v0001/request.txt"
    historical_manifest_path = root / "text/preparations/BODY/v0001/manifest.json"
    historical_request = historical_request_path.read_bytes()
    historical_manifest = historical_manifest_path.read_bytes()

    services.academic.import_answers(project_id, _answers(" atualizada"))
    services.academic.authorize_plan(project_id)
    services.academic.import_plan(
        project_id, b"# Plano academico V2\nDesenvolvimento atualizado e aprovado."
    )
    second_context = services.writing.create_context(
        project_id, contract_id="synthetic_demo"
    )
    acknowledgement = services.writing.acknowledge_context_by_id(
        project_id, second_context.id, b"Segundo contexto confirmado."
    )
    services.writing.confirm_context_by_id(
        project_id, second_context.id, acknowledgement.raw_version
    )
    second_start_preparation = services.writing.prepare_unit_for_context(
        project_id, second_context.id, "START"
    )
    second_start = services.writing.import_unit_by_preparation_id(
        project_id,
        second_start_preparation.id,
        b"Fundamento atualizado suficientemente desenvolvido para o segundo contexto.",
    )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            PreparationRepository,
            "latest_for_project_unit",
            lambda repository, _project_id, unit_id: repository.latest(
                second_context.id, unit_id
            ),
        )
        with pytest.raises(ConflictError) as captured:
            services.writing.prepare_unit_for_context(
                project_id, second_context.id, "BODY"
            )
    assert captured.value.code == "ARTIFACT_DESTINATION_CONFLICT"

    with services.database.connection() as connection:
        legacy = PreparationRepository(connection).latest(second_context.id, "BODY")
        assert legacy is not None
        legacy_run = connection.execute(
            "SELECT status, finished_at FROM stage_runs WHERE id = ?",
            (legacy.stage_run_id,),
        ).fetchone()
        assert legacy.version == 1
        assert legacy.request_artifact_id is None
        assert legacy.manifest_artifact_id is None
        assert dict(legacy_run) == {"status": "running", "finished_at": None}
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE stage_run_id = ?",
            (legacy.stage_run_id,),
        ).fetchone()[0] == 0

    command = [
        *_cli_base(tmp_path, repository_root),
        "writing",
        "unit",
        "recover",
        project_id,
        "BODY",
    ]
    assert main(command) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preparation"]["id"] == legacy.id
    assert payload["preparation"]["previous_version"] == 1
    assert payload["preparation"]["version"] == 2
    assert payload["stage_run"]["id"] == legacy.stage_run_id
    assert payload["stage_run"]["previous_status"] == "running"
    assert payload["stage_run"]["status"] == "done"
    assert payload["stage_run"]["finished_at"] is not None
    assert payload["recovered"] is True
    assert {artifact["relative_path"] for artifact in payload["artifacts"]} == {
        "text/preparations/BODY/v0002/request.txt",
        "text/preparations/BODY/v0002/manifest.json",
    }
    recovered, recovered_request = services.writing.unit_request_by_id(
        project_id, legacy.id
    )
    assert recovered.version == 2
    assert recovered.id == legacy.id
    assert recovered_request != historical_request
    assert historical_request_path.read_bytes() == historical_request
    assert historical_manifest_path.read_bytes() == historical_manifest
    assert (
        root / "text/preparations/BODY/v0002/request.txt"
    ).read_bytes() == recovered_request
    with services.database.connection() as connection:
        dependencies = PreparationRepository(connection).dependencies(recovered.id)
        assert len(dependencies) == 1
        assert dependencies[0].submission_id == second_start.id
        assert dependencies[0].submission_id != first_start.id
        assert connection.execute(
            "SELECT count(*) FROM writing_unit_preparations "
            "WHERE project_id = ? AND unit_id = 'BODY'",
            (project_id,),
        ).fetchone()[0] == 2
    assert services.recovery.recover(project_id) == []
    assert main(command) == 0
    repeated_payload = json.loads(capsys.readouterr().out)
    assert repeated_payload["preparation"]["id"] == legacy.id
    assert repeated_payload["preparation"]["previous_version"] == 2
    assert repeated_payload["preparation"]["version"] == 2
    assert repeated_payload["recovered"] is False
    assert (
        services.writing.prepare_unit_for_context(
            project_id, second_context.id, "BODY"
        )
        == recovered
    )
    assert first_body.version == 1


def test_cli_unit_recovery_propagates_specific_unrecoverable_state(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    crashing = _crashing_service(services, "preparation.request")
    with pytest.raises(RuntimeError):
        crashing.prepare_unit(project_id, "START")
    command = [
        *_cli_base(tmp_path, repository_root),
        "writing",
        "unit",
        "recover",
        project_id,
        "START",
    ]

    assert main(command) == 5
    assert "WRITING_PREPARATION_OUTPUT_MISSING" in capsys.readouterr().err
    assert main(command) == 5
    assert "WRITING_PREPARATION_OUTPUT_MISSING" in capsys.readouterr().err
    with services.database.connection() as connection:
        rows = connection.execute(
            "SELECT preparation.id, preparation.version, run.status "
            "FROM writing_unit_preparations AS preparation "
            "JOIN stage_runs AS run ON run.id = preparation.stage_run_id "
            "WHERE preparation.project_id = ? AND preparation.unit_id = 'START'",
            (project_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["status"] == "pending_retry"


@pytest.mark.parametrize(
    "checkpoint",
    [
        "submission.validation_report",
        "submission.accepted",
        "submission.citation_ledger",
    ],
)
def test_recovery_recomputes_submission_acceptance_and_ledger(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    checkpoint: str,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    services.writing.prepare_unit(project_id, "START")
    crashing = _crashing_service(services, checkpoint)
    raw = b"Porter (1980) sustenta um desenvolvimento textual valido e verificavel."
    with pytest.raises(RuntimeError):
        crashing.import_unit(project_id, "START", raw)
    result = services.recovery.recover(project_id)
    assert [item.status for item in result] == [RunStatus.DONE]
    submission, accepted = services.writing.unit(project_id, "START", version=1)
    assert accepted == raw
    assert submission.citation_ledger_artifact_id is not None
    assert services.writing.validate(project_id) == []
    assert services.recovery.recover(project_id) == []


def test_recovery_marks_incomplete_submission_pending_retry(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    services.writing.prepare_unit(project_id, "START")
    crashing = _crashing_service(services, "submission.raw")
    with pytest.raises(RuntimeError):
        crashing.import_unit(
            project_id, "START", "Conteúdo válido com extensão suficiente.".encode()
        )
    result = services.recovery.recover(project_id)
    assert [item.status for item in result] == [RunStatus.PENDING_RETRY]
    assert services.recovery.recover(project_id) == []


def test_recovery_reconciles_consolidation_manifest_and_members(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    _accept(services, project_id, "START", "Fundamentos iniciais com extensão válida.")
    _accept(
        services,
        project_id,
        "BODY",
        "Desenvolvimento central com extensão suficiente e continuidade comprovada.",
    )
    _accept(services, project_id, "END", "Síntese final válida e preservada.")
    crashing = _crashing_service(services, "consolidation.manifest")
    with pytest.raises(RuntimeError):
        crashing.consolidate(project_id)
    with pytest.raises(IntegrityError, match="recovery"):
        services.writing.consolidate(project_id)
    result = services.recovery.recover(project_id)
    assert [item.status for item in result] == [RunStatus.DONE]
    consolidation, content = services.writing.consolidated(project_id)
    assert content
    assert services.writing.consolidated(project_id, version=1)[0] == consolidation
    with services.database.connection() as connection:
        members = connection.execute(
            "SELECT count(*) FROM text_consolidation_members WHERE consolidation_id = ?",
            (consolidation.id,),
        ).fetchone()[0]
    assert members == 3


def test_recovery_handles_every_missing_m2_metadata_shape(
    services: ServiceBundle,
    project_config_path: Path,
) -> None:
    project_id = services.projects.create(project_config_path).id
    identities = (
        (CONTEXT_STAGE, "context:create"),
        (CONTEXT_STAGE, "context:ack:missing"),
        (CONTEXT_STAGE, "context:confirm:missing"),
        (TEXT_STAGE, "prepare:START"),
        (TEXT_STAGE, "import:START"),
        (TEXT_STAGE, "confirm:missing"),
        (CONSOLIDATION_STAGE, "consolidate"),
    )
    with services.database.connection() as connection:
        for version, (stage_id, unit_id) in enumerate(identities, start=1):
            run = services.writing.contexts.persistence.new_run(
                project_id=project_id,
                stage_id=stage_id,
                unit_id=unit_id,
                input_hash=f"{version:064x}",
                version=version,
            )
            with services.database.transaction(connection):
                services.writing.contexts.persistence.add_and_start(connection, run)

    results = services.recovery.recover(project_id)
    assert len(results) == len(identities)
    assert {item.status for item in results} == {RunStatus.PENDING_RETRY}


@pytest.mark.parametrize(
    "checkpoint",
    ["submission.confirmed_accepted", "submission.confirmed_citation_ledger"],
)
def test_recovery_completes_explicit_review_confirmation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    checkpoint: str,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    services.writing.prepare_unit(project_id, "START")
    review = services.writing.import_unit(
        project_id,
        "START",
        b"Alpha desenvolve o argumento de forma suficiente.\n\n"
        b"Alpha encerra o argumento com clareza.",
    )
    crashing = _crashing_service(services, checkpoint)
    with pytest.raises(RuntimeError):
        crashing.confirm_unit(project_id, "START", review.raw_version)
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE
    submission, accepted = services.writing.unit(project_id, "START", version=1)
    assert submission.disposition.value == "accepted"
    assert accepted.startswith(b"Alpha")


def test_recovery_blocks_divergent_context_bytes(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    crashing = _crashing_service(services, "context.artifacts")
    with pytest.raises(RuntimeError):
        crashing.create_context(project_id, contract_id="synthetic_demo")
    project = services.projects.get(project_id)
    package = services.store.resolve(project.artifact_root, "text/context/v0001/package.txt")
    package.write_bytes(b"corrupted")
    result = services.recovery.recover(project_id)
    assert result[0].status is RunStatus.BLOCKED


def test_recovery_rejects_invalid_preparation_provenance(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    _accept(services, project_id, "START", "Conteúdo inicial suficientemente desenvolvido.")
    crashing = _crashing_service(services, "preparation.manifest")
    with pytest.raises(RuntimeError):
        crashing.prepare_unit(project_id, "BODY")
    with services.database.connection() as connection:
        connection.execute("DELETE FROM writing_preparation_dependencies")
        connection.commit()
    assert services.recovery.recover(project_id)[0].status is RunStatus.BLOCKED


def test_recovery_materializes_rejected_submission_without_accepted_artifact(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    services.writing.prepare_unit(project_id, "START")
    crashing = _crashing_service(services, "submission.validation_report")
    with pytest.raises(RuntimeError):
        crashing.import_unit(project_id, "START", b"curto")
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE
    submission, raw = services.writing.unit(project_id, "START", raw=True)
    assert submission.disposition.value == "rejected"
    assert raw == b"curto"


@pytest.mark.parametrize("mode", ["corrupt", "divergent"])
def test_recovery_blocks_invalid_or_divergent_validation_report(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    mode: str,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    services.writing.prepare_unit(project_id, "START")
    crashing = _crashing_service(services, "submission.validation_report")
    with pytest.raises(RuntimeError):
        crashing.import_unit(
            project_id,
            "START",
            "Conteúdo suficientemente desenvolvido para validação objetiva.".encode(),
        )
    project = services.projects.get(project_id)
    report_path = services.store.resolve(
        project.artifact_root, "text/validation/START/raw-v0001.json"
    )
    if mode == "corrupt":
        replacement = b"not-json"
    else:
        value = json.loads(report_path.read_text(encoding="utf-8"))
        value["disposition"] = "rejected"
        replacement = canonical_json_bytes(value) + b"\n"
    report_path.write_bytes(replacement)
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE text_unit_submissions SET validation_report_sha256 = ?",
            (sha256_bytes(replacement),),
        )
        connection.commit()
    assert services.recovery.recover(project_id)[0].status is RunStatus.BLOCKED


def test_recovery_missing_context_snapshot_and_preparation_output_are_retryable(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    crashing_context = _crashing_service(services, "context.package")
    with pytest.raises(RuntimeError):
        crashing_context.create_context(project_id, contract_id="synthetic_demo")
    assert services.recovery.recover(project_id)[0].status is RunStatus.PENDING_RETRY

    # A separate project fixture is not needed: retry the same identity after restoring its run.
    with services.database.connection() as connection:
        run_id = connection.execute(
            "SELECT stage_run_id FROM writing_contexts WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE id = ?",
            (run_id,),
        )
        connection.commit()
    project = services.projects.get(project_id)
    package = services.store.resolve(project.artifact_root, "text/context/v0001/package.txt")
    package.unlink()
    assert services.recovery.recover(project_id)[0].status is RunStatus.PENDING_RETRY


def test_recovery_accepts_done_submission_target_for_orphan_confirmation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    accepted = _accept(
        services, project_id, "START", "Conteúdo aceito suficientemente desenvolvido."
    )
    run = services.writing.contexts.persistence.new_run(
        project_id=project_id,
        stage_id=TEXT_STAGE,
        unit_id=f"confirm:{accepted.id}",
        input_hash="f" * 64,
        version=1,
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        services.writing.contexts.persistence.add_and_start(connection, run)
    assert services.recovery.recover(project_id)[0].status is RunStatus.DONE


def test_recovery_requires_all_frozen_context_snapshots(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    crashing = _crashing_service(services, "context.artifacts")
    with pytest.raises(RuntimeError):
        crashing.create_context(project_id, contract_id="synthetic_demo")
    project = services.projects.get(project_id)
    frozen_prompt = services.store.resolve(
        project.artifact_root, "prompts/frozen/omega_writing/v0001.txt"
    )
    frozen_prompt.unlink()
    assert services.recovery.recover(project_id)[0].status is RunStatus.PENDING_RETRY


def test_recovery_missing_acknowledgement_and_preparation_outputs_are_retryable(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    services.writing.create_context(project_id, contract_id="synthetic_demo")
    crashing_ack = _crashing_service(services, "acknowledgement.raw")
    with pytest.raises(RuntimeError):
        crashing_ack.acknowledge_context(project_id, b"Contexto compreendido.")
    project = services.projects.get(project_id)
    raw_ack = services.store.resolve(
        project.artifact_root,
        "text/context/v0001/acknowledgement/raw-v0001.txt",
    )
    raw_ack.unlink()
    assert services.recovery.recover(project_id)[0].status is RunStatus.PENDING_RETRY

    with services.database.connection() as connection:
        acknowledgement_id = connection.execute(
            "SELECT id FROM writing_acknowledgements WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        confirm_run = services.writing.contexts.persistence.new_run(
            project_id=project_id,
            stage_id=CONTEXT_STAGE,
            unit_id=f"context:confirm:{acknowledgement_id}",
            input_hash="e" * 64,
            version=1,
        )
        with services.database.transaction(connection):
            services.writing.contexts.persistence.add_and_start(connection, confirm_run)
    confirmation_results = services.recovery.recover(project_id)
    assert confirmation_results[-1].status is RunStatus.PENDING_RETRY


def test_recovery_missing_preparation_manifest_is_retryable(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    crashing = _crashing_service(services, "preparation.request")
    with pytest.raises(RuntimeError):
        crashing.prepare_unit(project_id, "START")
    assert services.recovery.recover(project_id)[0].status is RunStatus.PENDING_RETRY
