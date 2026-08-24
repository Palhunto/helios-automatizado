from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_writing_flow import _confirmed_context

from ebook_pipeline.cli import main
from ebook_pipeline.core.errors import ArtifactError, IntegrityError
from ebook_pipeline.writing.models import SubmissionDisposition, TextUnitSubmission
from ebook_pipeline.writing.service import WritingService


def _prepare_and_import(
    services: ServiceBundle,
    project_id: str,
    raw: bytes,
    *,
    reprocess: bool,
) -> TextUnitSubmission:
    services.writing.prepare_unit(project_id, "START", reprocess=reprocess)
    return services.writing.import_unit(project_id, "START", raw)


def _cli_args(services: ServiceBundle) -> list[str]:
    return [
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
    ]


def test_validation_reports_follow_raw_versions_and_survive_restart(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    review = _prepare_and_import(
        services,
        project_id,
        (
            "Alpha desenvolve o argumento de forma suficientemente extensa.\n\n"
            "Alpha aprofunda a análise com continuidade clara.\n\n"
            "Ação conclusiva preserva a coerência do argumento."
        ).encode(),
        reprocess=False,
    )
    assert review.disposition is SubmissionDisposition.REVIEW_REQUIRED
    review_submission, review_content = services.writing.unit(
        project_id, "START", version=review.raw_version, raw=True
    )
    review_report = services.writing.unit_validation_report(project_id, review_submission)
    assert review_content.startswith(b"Alpha")
    assert review_report["disposition"] == "review_required"
    assert review_report["error_count"] == 0
    assert review_report["warning_count"] == 2
    assert isinstance(review_report["findings"], list)
    assert len(review_report["findings"]) == 2

    confirmed = services.writing.confirm_unit(project_id, "START", review.raw_version)
    assert confirmed.disposition is SubmissionDisposition.ACCEPTED
    # Confirmation is explicit; it does not rewrite the original validation evidence.
    assert services.writing.unit_validation_report(project_id, confirmed)["disposition"] == (
        "review_required"
    )

    rejected = _prepare_and_import(services, project_id, b"curto", reprocess=True)
    assert rejected.disposition is SubmissionDisposition.REJECTED
    rejected_report = services.writing.unit_validation_report(project_id, rejected)
    assert rejected_report["disposition"] == "rejected"
    assert isinstance(rejected_report["error_count"], int)
    assert rejected_report["error_count"] >= 1

    accepted = _prepare_and_import(
        services,
        project_id,
        b"Conteudo final suficientemente desenvolvido para aceitacao objetiva.",
        reprocess=True,
    )
    assert accepted.disposition is SubmissionDisposition.ACCEPTED
    accepted_report = services.writing.unit_validation_report(project_id, accepted)
    assert accepted_report["disposition"] == "accepted"
    assert accepted_report["error_count"] == 0
    assert accepted_report["warning_count"] == 0

    restarted = WritingService(
        services.config, services.database, services.store, services.academic
    )
    reloaded, _ = restarted.unit(project_id, "START", version=accepted.raw_version, raw=True)
    assert restarted.unit_validation_report(project_id, reloaded) == accepted_report


def test_validation_report_fails_closed_when_missing_divergent_or_absent(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    submission = _prepare_and_import(
        services,
        project_id,
        b"Conteudo valido com extensao suficiente para validacao objetiva.",
        reprocess=False,
    )
    assert submission.validation_report_artifact_id is not None
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE text_unit_submissions SET validation_report_artifact_id = NULL WHERE id = ?",
            (submission.id,),
        )
        connection.commit()
    with pytest.raises(IntegrityError) as missing:
        services.writing.unit_validation_report(project_id, submission)
    assert missing.value.code == "WRITING_VALIDATION_REPORT_MISSING"
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE text_unit_submissions SET validation_report_artifact_id = ? WHERE id = ?",
            (submission.validation_report_artifact_id, submission.id),
        )
        connection.commit()

    project = services.projects.get(project_id)
    with services.database.connection() as connection:
        artifact = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = ?",
            (submission.validation_report_artifact_id,),
        ).fetchone()
    assert artifact is not None
    report_path = services.store.resolve(project.artifact_root, str(artifact["relative_path"]))
    original = report_path.read_bytes()
    report_path.write_bytes(b"divergent")
    with pytest.raises(IntegrityError) as divergent:
        services.writing.unit_validation_report(project_id, submission)
    assert divergent.value.code == "WRITING_ARTIFACT_HASH_MISMATCH"
    report_path.write_bytes(original)
    report_path.unlink()
    with pytest.raises(ArtifactError) as absent:
        services.writing.unit_validation_report(project_id, submission)
    assert absent.value.code == "ARTIFACT_MISSING"


def test_cli_unit_show_includes_review_findings(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    review = _prepare_and_import(
        services,
        project_id,
        (
            "Alpha desenvolve o argumento de forma suficientemente extensa.\n\n"
            "Alpha aprofunda a análise com continuidade clara.\n\n"
            "Ação conclusiva preserva a coerência do argumento."
        ).encode(),
        reprocess=False,
    )
    assert (
        main(
            [
                *_cli_args(services),
                "writing",
                "unit",
                "show",
                project_id,
                "START",
                "--version",
                str(review.raw_version),
                "--raw",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["submission"]["id"] == review.id
    assert payload["validation_report"]["disposition"] == "review_required"
    assert payload["validation_report"]["warning_count"] == 2
    assert payload["validation_report"]["findings"]
