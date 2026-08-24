from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from ebook_pipeline.core.hashing import canonical_hash
from ebook_pipeline.core.models import StageRun, ValidationIssue

EvidenceVerifier = Callable[[sqlite3.Connection, StageRun, str], str | None]


@dataclass(frozen=True, slots=True)
class DatabaseEvidenceRule:
    stage_id: str
    unit_prefix: str
    verifier: EvidenceVerifier

    def entity_id(self, run: StageRun) -> str | None:
        if run.stage_id != self.stage_id or not run.unit_id.startswith(self.unit_prefix):
            return None
        entity_id = run.unit_id.removeprefix(self.unit_prefix)
        return entity_id or None


class DoneRunEvidencePolicy:
    """Validate durable evidence for completed operations.

    Artifacts are required by default. A database-only operation must be declared here and prove
    its durable entity, source artifact and deterministic run identity.
    """

    def __init__(self) -> None:
        self.database_rules = (
            DatabaseEvidenceRule(
                stage_id="writing_context_load",
                unit_prefix="context:confirm:",
                verifier=self._verify_writing_context_confirmation,
            ),
        )

    def validate(
        self,
        connection: sqlite3.Connection,
        run: StageRun,
        *,
        has_artifact: bool,
    ) -> ValidationIssue | None:
        if has_artifact:
            return None
        for rule in self.database_rules:
            entity_id = rule.entity_id(run)
            if entity_id is None:
                continue
            reason = rule.verifier(connection, run, entity_id)
            if reason is None:
                return None
            return ValidationIssue(
                "DONE_RUN_DATABASE_EVIDENCE_INVALID",
                f"Done stage run {run.id!r} has invalid database evidence: {reason}",
            )
        return ValidationIssue(
            "DONE_RUN_WITHOUT_ARTIFACT",
            f"Done stage run {run.id!r} has no registered artifact",
        )

    @staticmethod
    def _verify_writing_context_confirmation(
        connection: sqlite3.Connection, run: StageRun, acknowledgement_id: str
    ) -> str | None:
        row = connection.execute(
            "SELECT acknowledgement.id, acknowledgement.raw_sha256, "
            "acknowledgement.raw_artifact_id, acknowledgement.confirmed_at, "
            "source_run.status AS source_run_status, raw_artifact.project_id AS artifact_project, "
            "raw_artifact.sha256 AS artifact_sha256 "
            "FROM writing_acknowledgements AS acknowledgement "
            "LEFT JOIN stage_runs AS source_run ON source_run.id = acknowledgement.stage_run_id "
            "LEFT JOIN artifacts AS raw_artifact "
            "ON raw_artifact.id = acknowledgement.raw_artifact_id "
            "WHERE acknowledgement.id = ? AND acknowledgement.project_id = ?",
            (acknowledgement_id, run.project_id),
        ).fetchone()
        if row is None:
            return "confirmed acknowledgement is missing"
        if row["confirmed_at"] is None:
            return "acknowledgement is not confirmed"
        if row["raw_artifact_id"] is None or row["artifact_project"] != run.project_id:
            return "acknowledgement raw artifact binding is missing"
        if row["source_run_status"] != "done":
            return "acknowledgement import run is not done"
        if row["artifact_sha256"] != row["raw_sha256"]:
            return "acknowledgement raw artifact hash differs from provenance"
        expected_hash = canonical_hash(
            {
                "acknowledgement_id": acknowledgement_id,
                "operation": "writing.context.confirm",
                "raw_sha256": row["raw_sha256"],
            }
        )
        if run.input_hash != expected_hash:
            return "confirmation run input hash differs from the durable decision"
        return None
