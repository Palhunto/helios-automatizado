from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from conftest import ServiceBundle
from test_browser_automation import _automation_context

from ebook_pipeline.browser.composer_probe import ComposerProbeService
from ebook_pipeline.browser.fingerprints import transport_fingerprint
from ebook_pipeline.browser.models import ComposerProbeCaseResult


class RecordingComposerProbeAdapter:
    def __init__(self) -> None:
        self.payloads: Mapping[str, str] | None = None

    def composer_probe(
        self, payloads: Mapping[str, str]
    ) -> dict[str, ComposerProbeCaseResult]:
        self.payloads = payloads
        return {
            name: ComposerProbeCaseResult(
                expected_length=len(payload),
                observed_length=len(payload),
                expected_fingerprint=transport_fingerprint(payload),
                observed_fingerprint=transport_fingerprint(payload),
                stable_read_1=transport_fingerprint(payload),
                stable_read_2=transport_fingerprint(payload),
                editor_metadata={"tag_name": "textarea", "focused": True},
            )
            for name, payload in payloads.items()
        }


def _operational_counts(services: ServiceBundle) -> dict[str, int]:
    tables = (
        "projects",
        "stage_runs",
        "artifacts",
        "browser_conversations",
        "browser_interactions",
        "browser_interaction_events",
    )
    with services.database.connection() as connection:
        return {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_composer_probe_reads_exact_context_artifact_without_database_mutation(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    context, request_bytes = services.writing.context_package_by_id(project_id, context_id)
    assert context.package_artifact_id is not None
    before_counts = _operational_counts(services)
    before_database = services.database.path.read_bytes()
    wal_path = services.database.path.with_name(f"{services.database.path.name}-wal")
    before_wal = wal_path.read_bytes() if wal_path.exists() else None
    adapter = RecordingComposerProbeAdapter()

    result = ComposerProbeService(
        services.config,
        services.database,
        services.store,
        adapter,
    ).run(project_id, context.package_artifact_id)

    assert result["database_mode"] == "read_only"
    assert result["context_id"] == context_id
    assert result["request_artifact_id"] == context.package_artifact_id
    assert adapter.payloads is not None
    assert list(adapter.payloads) == [
        "short",
        "multiline_unicode",
        "large",
        "writing_context_request",
    ]
    assert adapter.payloads["writing_context_request"].encode("utf-8") == request_bytes
    assert _operational_counts(services) == before_counts
    assert services.database.path.read_bytes() == before_database
    after_wal = wal_path.read_bytes() if wal_path.exists() else None
    assert after_wal == before_wal


def test_composer_probe_artifact_only_skips_synthetic_payloads_without_database_mutation(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    context, request_bytes = services.writing.context_package_by_id(project_id, context_id)
    assert context.package_artifact_id is not None
    before_counts = _operational_counts(services)
    before_database = services.database.path.read_bytes()
    adapter = RecordingComposerProbeAdapter()

    result = ComposerProbeService(
        services.config,
        services.database,
        services.store,
        adapter,
    ).run(project_id, context.package_artifact_id, artifact_only=True)

    assert result["artifact_only"] is True
    assert result["database_mode"] == "read_only"
    assert adapter.payloads is not None
    assert list(adapter.payloads) == ["writing_context_request"]
    assert adapter.payloads["writing_context_request"].encode("utf-8") == request_bytes
    assert _operational_counts(services) == before_counts
    assert services.database.path.read_bytes() == before_database
