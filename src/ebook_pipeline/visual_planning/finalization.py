from __future__ import annotations

import sqlite3
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import RunStatus, ValidationIssue
from ebook_pipeline.storage.repositories import ProjectRepository, StageRunRepository
from ebook_pipeline.visual_planning.anchor_repository import (
    AnchorRepository,
    FinalizationRepository,
)
from ebook_pipeline.visual_planning.enrichment import VisualAnchorService
from ebook_pipeline.visual_planning.models import (
    VisualAnchor,
    VisualFinalization,
    VisualPlan,
    VisualPlanSources,
)
from ebook_pipeline.visual_planning.operation_io import (
    log_result,
    read_owned,
    record_failure,
    resume,
    verify_files,
)
from ebook_pipeline.visual_planning.repositories import VisualPlanRepository

if TYPE_CHECKING:
    from ebook_pipeline.visual_planning.service import VisualPlanningService

FINALIZE_STAGE = "visual_validation"


def finalization_identity(plan: VisualPlan, anchors: list[VisualAnchor]) -> str:
    return canonical_hash(
        {
            "schema": "helios_visual_manifest@1",
            "project_id": plan.project_id,
            "plan_id": plan.id,
            "plan_input_hash": plan.input_hash,
            "anchors": [
                {"id": a.id, "input_hash": a.input_hash, "report_sha256": a.report_sha256}
                for a in anchors
            ],
        }
    )


class VisualFinalizationService:
    def __init__(self, visual: VisualPlanningService) -> None:
        self.visual = visual
        self.anchors = VisualAnchorService(visual)

    def finalize(
        self, project_id: str, plan_version: int | None = None
    ) -> tuple[VisualFinalization, dict[str, Any]]:
        visual = self.visual
        sources = visual.require_plan(project_id, plan_version)
        plan, figures, _, _ = sources
        with visual.database.connection() as connection:
            selected = [AnchorRepository(connection).latest(f.id) for f in figures]
        if any(a is None or a.disposition != "valid" for a in selected):
            raise ConflictError(
                "VISUAL_FINALIZE_ANCHORS_REQUIRED",
                "Every figure requires a valid latest anchor",
                evidence={
                    "figure_ids": [
                        f.id
                        for f, a in zip(figures, selected, strict=True)
                        if a is None or a.disposition != "valid"
                    ]
                },
            )
        anchors = [a for a in selected if a is not None]
        for anchor in anchors:
            self.anchors.validate_one(anchor, sources=sources)
        identity = finalization_identity(plan, anchors)
        with visual.database.connection() as connection, visual.database.transaction(connection):
            visual.assert_current(connection, plan)
            self._assert_selection(connection, anchors)
            repository = FinalizationRepository(connection)
            history = repository.list(project_id)
            item = next((f for f in history if f.input_hash == identity), None)
            if item is None:
                previous = history[-1] if history else None
                version = 1 if previous is None else previous.accepted_version + 1
                run = visual.persistence.new_run(
                    project_id=project_id,
                    stage_id=FINALIZE_STAGE,
                    unit_id="finalize",
                    input_hash=identity,
                    version=version,
                    supersedes_run_id=None if previous is None else previous.stage_run_id,
                )
                visual.persistence.add_and_start(connection, run)
                item = VisualFinalization(
                    id=new_id(),
                    project_id=project_id,
                    visual_plan_id=plan.id,
                    stage_run_id=run.id,
                    accepted_version=version,
                    input_hash=identity,
                    manifest_artifact_id=None,
                    manifest_sha256=None,
                    created_at=utc_now(),
                    accepted_at=None,
                )
                repository.add(item, anchors)
            run = resume(visual, connection, item.stage_run_id)
            if (run.status is RunStatus.DONE) != (item.accepted_at is not None):
                raise IntegrityError(
                    "VISUAL_FINALIZE_STATE_INVALID", "Finalization states disagree"
                )
        if run.status is RunStatus.DONE:
            return item, self.validate_one(item)
        try:
            manifest = self._manifest(item, anchors, sources=sources)
            with visual.database.connection() as connection:
                project = ProjectRepository(connection).get(project_id)
            stored = visual.store.write_bytes(
                project.artifact_root,
                f"visual-plan/accepted/v{item.accepted_version:04d}.json",
                canonical_json_bytes(manifest) + b"\n",
            )
            visual.checkpoint("visual_finalize.manifest")
            with (
                visual.database.connection() as connection,
                visual.database.transaction(connection),
            ):
                visual.assert_current(connection, plan)
                self._assert_selection(connection, anchors)
                current = FinalizationRepository(connection).get(item.id)
                run = StageRunRepository(connection).get(item.stage_run_id)
                if current.accepted_at is not None or run.status is not RunStatus.RUNNING:
                    raise ConflictError("VISUAL_FINALIZE_CONCURRENT_UPDATE", "Finalization changed")
                if self._manifest(item, anchors) != manifest:
                    raise IntegrityError("VISUAL_FINALIZE_SOURCE_CHANGED", "Visual sources changed")
                verify_files(visual, project, stored)
                artifact = visual.persistence.register_artifact(
                    connection, project, run, "visual_manifest", stored, item.accepted_version
                )
                item = replace(
                    item,
                    manifest_artifact_id=artifact.id,
                    manifest_sha256=artifact.sha256,
                    accepted_at=utc_now(),
                )
                FinalizationRepository(connection).complete(item)
                visual.persistence.finish(connection, run.id)
            log_result(run, "accepted")
            return item, manifest
        except Exception as exc:
            record_failure(visual, item.stage_run_id, exc)
            raise

    def show(
        self, project_id: str, version: int | None = None
    ) -> tuple[VisualFinalization, dict[str, Any]]:
        with self.visual.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            candidates = FinalizationRepository(connection).list(project_id)
        if version is not None:
            candidates = [f for f in candidates if f.accepted_version == version]
        if not candidates:
            raise NotFoundError("VISUAL_FINALIZATION_NOT_FOUND", "No visual finalization exists")
        return candidates[-1], self.validate_one(candidates[-1])

    def status(self, project_id: str) -> dict[str, Any]:
        with self.visual.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            history = FinalizationRepository(connection).list(project_id)
            if not history:
                return {"project_id": project_id, "status": "missing", "current": False}
            item = history[-1]
            anchors = FinalizationRepository(connection).anchors(item.id)
            run = StageRunRepository(connection).get(item.stage_run_id)
            selection_current = all(
                AnchorRepository(connection).latest(a.figure_id) == a for a in anchors
            )
        source = self.visual.status(project_id)
        issues: list[str] = []
        if run.status is RunStatus.DONE:
            try:
                self.validate_one(item)
            except HeliosError as exc:
                issues.append(exc.code)
        current = (
            source.get("current", False)
            and source.get("plan_id") == item.visual_plan_id
            and selection_current
            and run.status is RunStatus.DONE
            and not issues
        )
        return {
            "project_id": project_id,
            "finalization_id": item.id,
            "accepted_version": item.accepted_version,
            "current": bool(current),
            "stage_run_status": run.status.value,
            "anchors_current": selection_current,
            "integrity_errors": issues,
            "recovery_required": run.status is not RunStatus.DONE,
        }

    def validate_one(self, item: VisualFinalization) -> dict[str, Any]:
        visual = self.visual
        with visual.database.connection() as connection:
            run = StageRunRepository(connection).get(item.stage_run_id)
            if run.status is not RunStatus.DONE or item.accepted_at is None:
                raise IntegrityError("VISUAL_FINALIZATION_INCOMPLETE", "Finalize requires recovery")
            anchors = FinalizationRepository(connection).anchors(item.id)
            stored = read_owned(
                visual,
                connection,
                run,
                item.manifest_artifact_id,
                item.manifest_sha256,
                "visual_manifest",
            )
            plan = VisualPlanRepository(connection).get(item.visual_plan_id)
        if (
            run.project_id != item.project_id
            or run.stage_id != FINALIZE_STAGE
            or run.unit_id != "finalize"
            or run.version != item.accepted_version
            or item.input_hash != finalization_identity(plan, anchors)
            or run.input_hash != item.input_hash
        ):
            raise IntegrityError(
                "VISUAL_FINALIZATION_IDENTITY_INVALID", "Finalize identity differs"
            )
        manifest = self._manifest(item, anchors)
        if stored != canonical_json_bytes(manifest) + b"\n":
            raise IntegrityError(
                "VISUAL_MANIFEST_MISMATCH", "Accepted manifest differs from evidence"
            )
        return manifest

    def validate(self, project_id: str) -> list[ValidationIssue]:
        with self.visual.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            anchors = [
                AnchorRepository(connection).get(str(row[0]))
                for row in connection.execute(
                    "SELECT id FROM visual_anchors WHERE project_id = ?", (project_id,)
                )
            ]
            history = FinalizationRepository(connection).list(project_id)
        issues: list[ValidationIssue] = []
        sources_by_plan: dict[str, VisualPlanSources] = {}
        for anchor in anchors:
            try:
                if anchor.visual_plan_id not in sources_by_plan:
                    with self.visual.database.connection() as connection:
                        plan = VisualPlanRepository(connection).get(anchor.visual_plan_id)
                    sources_by_plan[plan.id] = self.visual.require_plan(
                        project_id, plan.raw_version, current=False
                    )
                self.anchors.validate_one(anchor, sources=sources_by_plan[anchor.visual_plan_id])
            except HeliosError as exc:
                issues.append(ValidationIssue(exc.code, f"{anchor.id}: {exc.message}"))
        for item in history:
            try:
                self.validate_one(item)
            except HeliosError as exc:
                issues.append(ValidationIssue(exc.code, f"{item.id}: {exc.message}"))
        # A structurally invalid raw or rejected anchor is preserved evidence, not corruption.
        return issues

    def _manifest(
        self,
        item: VisualFinalization,
        anchors: list[VisualAnchor],
        *,
        sources: VisualPlanSources | None = None,
    ) -> dict[str, Any]:
        if sources is None:
            with self.visual.database.connection() as connection:
                plan = VisualPlanRepository(connection).get(item.visual_plan_id)
            sources = self.visual.require_plan(item.project_id, plan.raw_version, current=False)
        plan, figures, pagination, _ = sources
        if [a.figure_id for a in anchors] != [f.id for f in figures] or any(
            a.visual_plan_id != plan.id or a.disposition != "valid" for a in anchors
        ):
            raise IntegrityError("VISUAL_FINALIZE_COVERAGE_INVALID", "Anchor coverage differs")
        entries = []
        for figure, anchor in zip(figures, anchors, strict=True):
            report = self.anchors.validate_one(anchor, sources=sources)
            entries.append(
                {
                    **asdict(figure),
                    "visual_id": figure.id,
                    "resolved_unit_id": anchor.unit_id,
                    "anchor": {
                        **asdict(anchor),
                        **report["candidate"],
                        "validation": report["validation"],
                        "validation_rule_version": report["validation_rule_version"],
                    },
                }
            )
        return {
            "schema": "helios_visual_manifest@1",
            "finalization_id": item.id,
            "project_id": item.project_id,
            "accepted_version": item.accepted_version,
            "input_hash": item.input_hash,
            "created_at": item.created_at,
            "plan": asdict(plan),
            "eligible_pages": [page for page in pagination["pages"] if page["eligible"]],
            "expected_figure_count": plan.expected_figure_count,
            "figure_count": len(figures),
            "figures": entries,
        }

    @staticmethod
    def _assert_selection(connection: sqlite3.Connection, anchors: list[VisualAnchor]) -> None:
        if any(AnchorRepository(connection).latest(a.figure_id) != a for a in anchors):
            raise ConflictError("VISUAL_FINALIZE_ANCHORS_CHANGED", "Anchor selection changed")
