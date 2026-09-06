from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Project, RunStatus, StageRun, StoredFile, ValidationIssue
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.pagination.models import VisualPaginationSnapshot
from ebook_pipeline.pagination.repositories import PaginationRepository
from ebook_pipeline.pagination.service import PaginationService
from ebook_pipeline.prompts import PromptRegistry, ResolvedPrompt
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.visual_planning.models import (
    BoundVisualFigure,
    ValidationFinding,
    VisualFigure,
    VisualPlan,
)
from ebook_pipeline.visual_planning.operation_io import verify_files
from ebook_pipeline.visual_planning.parser import parse_visual_plan
from ebook_pipeline.visual_planning.repositories import (
    VisualFigureRepository,
    VisualPlanRepository,
)
from ebook_pipeline.visual_planning.validators import bind_figures_to_pagination
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.repositories import ConsolidationRepository

VISUAL_PLAN_STAGE = "visual_plan_import"
VISUAL_PLAN_UNIT = "plan"
VISUAL_PLAN_PROMPT_ID = "omega_visual_planning"
VISUAL_PLAN_PROMPT_VERSION = 2


@dataclass(frozen=True, slots=True)
class _VisualPlanInputs:
    project: Project
    snapshot: VisualPaginationSnapshot
    manifest: dict[str, Any]
    prompt: ResolvedPrompt


class VisualPlanningService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        pagination: PaginationService,
        *,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.database = database
        self.store = store
        self.pagination = pagination
        self.persistence = WritingPersistence(config, database, store, fault_hook)
        self.prompts = PromptRegistry(config.prompt_registry)
        self.state_machine = StateMachine()
        self.fault_hook = fault_hook

    def import_raw(self, project_id: str, raw: bytes) -> tuple[VisualPlan, dict[str, Any]]:
        inputs = self._load_inputs(project_id)
        raw_sha256 = sha256_bytes(raw)
        input_hash = canonical_hash(
            {
                "operation": "visual_plan_import@1",
                "pagination_snapshot_id": inputs.snapshot.id,
                "pagination_manifest_sha256": inputs.snapshot.manifest_sha256,
                "project_id": project_id,
                "prompt_id": inputs.prompt.id,
                "prompt_sha256": inputs.prompt.sha256,
                "prompt_version": inputs.prompt.version,
                "raw_sha256": raw_sha256,
                "text_sha256": inputs.snapshot.text_sha256,
            }
        )
        plan, done = self._reserve(inputs, raw_sha256, input_hash)
        if done:
            return plan, self._validate_one(plan)
        try:
            raw_path = f"visual-plan/raw/v{plan.raw_version:04d}.txt"
            raw_stored = self.store.write_bytes(inputs.project.artifact_root, raw_path, raw)
            self._checkpoint("visual_plan.raw")
            prompt_path = f"prompts/frozen/{inputs.prompt.id}/v{inputs.prompt.version:04d}.txt"
            prompt_stored = self.store.write_bytes(
                inputs.project.artifact_root, prompt_path, inputs.prompt.content
            )
            self._checkpoint("visual_plan.prompt")

            parsed = parse_visual_plan(raw)
            bound: tuple[BoundVisualFigure, ...] = ()
            findings = parsed.findings
            if not findings:
                bound, findings = bind_figures_to_pagination(parsed.figures, inputs.manifest)
            disposition = "valid" if not findings else "invalid"
            report = self._report(plan, inputs, parsed.observed_figure_count, bound, findings)
            report_bytes = canonical_json_bytes(report) + b"\n"
            report_path = f"visual-plan/validation/raw-v{plan.raw_version:04d}.json"
            report_stored = self.store.write_bytes(
                inputs.project.artifact_root,
                report_path,
                report_bytes,
                validator=self._validate_json_file,
            )
            self._checkpoint("visual_plan.validation")

            with self.database.connection() as connection, self.database.transaction(connection):
                current = VisualPlanRepository(connection).get(plan.id)
                run = StageRunRepository(connection).get(plan.stage_run_id)
                if run.status is not RunStatus.RUNNING:
                    raise IntegrityError(
                        "VISUAL_PLAN_RUN_NOT_RUNNING",
                        "Visual plan import cannot finalize from its current StageRun state",
                    )
                self._assert_sources_unchanged(connection, current)
                verify_files(self, inputs.project, raw_stored, report_stored, prompt_stored)
                raw_artifact = self.persistence.register_artifact(
                    connection,
                    inputs.project,
                    run,
                    "visual_plan_raw",
                    raw_stored,
                    plan.raw_version,
                )
                report_artifact = self.persistence.register_artifact(
                    connection,
                    inputs.project,
                    run,
                    "visual_plan_validation_report",
                    report_stored,
                    plan.raw_version,
                )
                self._ensure_prompt_artifact(
                    connection, inputs.project, run, inputs.prompt, prompt_stored
                )
                if disposition == "valid":
                    figures = self._materialize_figures(current, bound)
                    repository = VisualFigureRepository(connection)
                    for figure, binding in zip(figures, bound, strict=True):
                        repository.add(figure, binding.page_unit_span_orders)
                completed = VisualPlanRepository(connection).complete(
                    current,
                    disposition=disposition,
                    raw_artifact_id=raw_artifact.id,
                    report_artifact_id=report_artifact.id,
                    report_sha256=report_stored.sha256,
                    figure_count=parsed.observed_figure_count,
                    validated_at=utc_now(),
                )
                self.persistence.finish(connection, run.id)
            self._validate_one(completed)
            return completed, report
        except Exception as exc:
            self._record_retry(plan, exc)
            raise

    def show(
        self, project_id: str, version: int | None = None
    ) -> tuple[VisualPlan, list[VisualFigure], dict[str, Any], bytes]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            plans = VisualPlanRepository(connection)
            plan = (
                plans.latest(project_id)
                if version is None
                else plans.by_version(project_id, version)
            )
            if plan is None or plan.disposition == "processing":
                raise NotFoundError(
                    "VISUAL_PLAN_NOT_FOUND", "No completed visual plan import was found"
                )
            project = ProjectRepository(connection).get(project_id)
            figures = VisualFigureRepository(connection).list(plan.id)
            prompt_path = f"prompts/frozen/{plan.prompt_id}/v{plan.prompt_version:04d}.txt"
            prompt_artifact = ArtifactRepository(connection).get_by_path(
                plan.project_id, prompt_path
            )
            if (
                prompt_artifact is None
                or prompt_artifact.artifact_type != "prompt_snapshot"
                or prompt_artifact.sha256 != plan.prompt_sha256
            ):
                raise IntegrityError(
                    "VISUAL_PLAN_PROMPT_ARTIFACT_INVALID",
                    "Frozen visual-planning prompt artifact is missing or inconsistent",
                )
            self.persistence.read_artifact(
                connection,
                project,
                prompt_artifact.id,
                expected_sha256=plan.prompt_sha256,
            )
            assert plan.validation_report_artifact_id is not None
            assert plan.raw_artifact_id is not None
            report_bytes = self.persistence.read_artifact(
                connection,
                project,
                plan.validation_report_artifact_id,
                expected_sha256=plan.validation_report_sha256,
            )
            raw = self.persistence.read_artifact(
                connection, project, plan.raw_artifact_id, expected_sha256=plan.raw_sha256
            )
        try:
            report = json.loads(report_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "VISUAL_PLAN_REPORT_INVALID", "Visual plan validation report is not JSON"
            ) from exc
        return plan, figures, report, raw

    def status(self, project_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            plan = VisualPlanRepository(connection).latest(project_id)
            if plan is None:
                return {"project_id": project_id, "status": "missing"}
            run = StageRunRepository(connection).get(plan.stage_run_id)
            latest_snapshot = PaginationRepository(connection).latest(project_id)
            latest_consolidation = ConsolidationRepository(connection).latest(project_id)
        prompt = self.prompts.resolve(VISUAL_PLAN_PROMPT_ID, VISUAL_PLAN_PROMPT_VERSION)
        pagination_status = self.pagination.status(project_id)
        stale_reasons: list[str] = []
        if latest_snapshot is None or plan.pagination_snapshot_id != latest_snapshot.id:
            stale_reasons.append("pagination_snapshot_changed")
        if not pagination_status.get("current", False):
            stale_reasons.append("pagination_snapshot_stale")
        if latest_consolidation is None or plan.consolidation_id != latest_consolidation.id:
            stale_reasons.append("consolidation_changed")
        if plan.prompt_sha256 != prompt.sha256:
            stale_reasons.append("prompt_changed")
        return {
            "project_id": project_id,
            "plan_id": plan.id,
            "raw_version": plan.raw_version,
            "disposition": plan.disposition,
            "stage_run_status": run.status.value,
            "current": (
                run.status is RunStatus.DONE and plan.disposition == "valid" and not stale_reasons
            ),
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "recovery_required": run.status is not RunStatus.DONE,
            "expected_figure_count": plan.expected_figure_count,
            "figure_count": plan.figure_count,
        }

    def validate(self, project_id: str) -> list[ValidationIssue]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            plans = VisualPlanRepository(connection).list(project_id)
        issues: list[ValidationIssue] = []
        for plan in plans:
            try:
                self._validate_one(plan)
            except HeliosError as exc:
                issues.append(ValidationIssue(exc.code, exc.message))
        return issues

    def list_figures(
        self, project_id: str, plan_version: int | None = None
    ) -> tuple[VisualPlan, list[VisualFigure]]:
        with self.database.connection() as connection:
            plans = VisualPlanRepository(connection)
            plan = (
                plans.latest(project_id)
                if plan_version is None
                else plans.by_version(project_id, plan_version)
            )
            if plan is None:
                raise NotFoundError("VISUAL_PLAN_NOT_FOUND", "Visual plan was not found")
            return plan, VisualFigureRepository(connection).list(plan.id)

    def show_figure(self, project_id: str, figure_id: str) -> VisualFigure:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            figure = VisualFigureRepository(connection).get(figure_id)
            if figure.project_id != project_id:
                raise NotFoundError(
                    "VISUAL_FIGURE_NOT_FOUND", f"Visual figure {figure_id!r} was not found"
                )
            return figure

    def _load_inputs(self, project_id: str) -> _VisualPlanInputs:
        pagination_status = self.pagination.status(project_id)
        if not pagination_status.get("current", False):
            raise ConflictError(
                "VISUAL_PLAN_PAGINATION_NOT_CURRENT",
                "Visual plan import requires the current canonical pagination snapshot",
                evidence={"pagination_status": pagination_status},
            )
        snapshot, manifest = self.pagination.show(project_id)
        if manifest.get("schema") != "helios_pagination_snapshot@1":
            raise IntegrityError(
                "VISUAL_PLAN_PAGINATION_SCHEMA_INVALID",
                "Visual plan import requires helios_pagination_snapshot@1",
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
        prompt = self.prompts.resolve(VISUAL_PLAN_PROMPT_ID, VISUAL_PLAN_PROMPT_VERSION)
        return _VisualPlanInputs(project, snapshot, manifest, prompt)

    def _reserve(
        self, inputs: _VisualPlanInputs, raw_sha256: str, input_hash: str
    ) -> tuple[VisualPlan, bool]:
        snapshot = inputs.snapshot
        assert snapshot.manifest_artifact_id is not None
        assert snapshot.manifest_sha256 is not None
        with self.database.connection() as connection, self.database.transaction(connection):
            plans = VisualPlanRepository(connection)
            existing = plans.by_input_hash(inputs.project.id, input_hash)
            if existing is not None:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is RunStatus.DONE:
                    if existing.disposition == "processing":
                        raise IntegrityError(
                            "VISUAL_PLAN_RECOVERY_EVIDENCE_INCOMPLETE",
                            "Completed run still points to a processing visual plan",
                        )
                    return existing, True
                if existing.disposition != "processing":
                    raise IntegrityError(
                        "VISUAL_PLAN_RECOVERY_STATE_INVALID",
                        "Incomplete run already points to a completed visual plan",
                    )
                if run.status is RunStatus.PENDING_RETRY:
                    running = self.state_machine.transition(run, RunStatus.RUNNING)
                    StageRunRepository(connection).update(running, expected_status=run.status)
                elif run.status is not RunStatus.RUNNING:
                    raise IntegrityError(
                        "VISUAL_PLAN_RECOVERY_REQUIRED",
                        f"Visual plan run in state {run.status.value!r} cannot resume locally",
                    )
                return existing, False
            latest = plans.latest(inputs.project.id)
            version = 1 if latest is None else latest.raw_version + 1
            run = self.persistence.new_run(
                project_id=inputs.project.id,
                stage_id=VISUAL_PLAN_STAGE,
                unit_id=VISUAL_PLAN_UNIT,
                input_hash=input_hash,
                version=version,
                supersedes_run_id=None if latest is None else latest.stage_run_id,
            )
            running = self.persistence.add_and_start(connection, run)
            plan = VisualPlan(
                id=new_id(),
                project_id=inputs.project.id,
                stage_run_id=running.id,
                raw_version=version,
                disposition="processing",
                input_hash=input_hash,
                pagination_snapshot_id=snapshot.id,
                pagination_manifest_artifact_id=snapshot.manifest_artifact_id,
                pagination_manifest_sha256=snapshot.manifest_sha256,
                consolidation_id=snapshot.consolidation_id,
                context_id=snapshot.context_id,
                production_set_hash=snapshot.production_set_hash,
                text_artifact_id=snapshot.text_artifact_id,
                text_sha256=snapshot.text_sha256,
                consolidation_manifest_artifact_id=snapshot.consolidation_manifest_artifact_id,
                consolidation_manifest_sha256=snapshot.consolidation_manifest_sha256,
                prompt_id=inputs.prompt.id,
                prompt_version=inputs.prompt.version,
                prompt_sha256=inputs.prompt.sha256,
                raw_sha256=raw_sha256,
                raw_artifact_id=None,
                validation_report_sha256=None,
                validation_report_artifact_id=None,
                expected_figure_count=snapshot.eligible_page_count,
                figure_count=0,
                created_at=utc_now(),
                validated_at=None,
            )
            plans.add(plan)
            return plan, False

    def _materialize_figures(
        self, plan: VisualPlan, bound: tuple[BoundVisualFigure, ...]
    ) -> tuple[VisualFigure, ...]:
        figures: list[VisualFigure] = []
        for binding in bound:
            editorial = binding.editorial
            figures.append(
                VisualFigure(
                    id=new_id(),
                    project_id=plan.project_id,
                    visual_plan_id=plan.id,
                    pagination_snapshot_id=plan.pagination_snapshot_id,
                    figure_order=binding.figure_order,
                    **asdict(editorial),
                    document_page_number=binding.document_page_number,
                    eligible_page_number=binding.eligible_page_number,
                    page_key=binding.page_key,
                    chapter_id=binding.chapter_id,
                    editorial_sha256=canonical_hash(asdict(editorial)),
                    page_unit_ids=binding.page_unit_ids,
                )
            )
        return tuple(figures)

    def _report(
        self,
        plan: VisualPlan,
        inputs: _VisualPlanInputs,
        actual_figure_count: int,
        bound: tuple[BoundVisualFigure, ...],
        findings: tuple[ValidationFinding, ...],
    ) -> dict[str, Any]:
        return {
            "schema": "helios_visual_plan_validation@1",
            "project_id": plan.project_id,
            "visual_plan_id": plan.id,
            "raw_version": plan.raw_version,
            "raw_sha256": plan.raw_sha256,
            "valid": not findings,
            "expected_figure_count": plan.expected_figure_count,
            "actual_figure_count": actual_figure_count,
            "pagination": {
                "snapshot_id": plan.pagination_snapshot_id,
                "manifest_artifact_id": plan.pagination_manifest_artifact_id,
                "manifest_sha256": plan.pagination_manifest_sha256,
            },
            "consolidation": {
                "id": plan.consolidation_id,
                "text_artifact_id": plan.text_artifact_id,
                "text_sha256": plan.text_sha256,
                "manifest_artifact_id": plan.consolidation_manifest_artifact_id,
                "manifest_sha256": plan.consolidation_manifest_sha256,
            },
            "prompt": {
                "id": inputs.prompt.id,
                "version": inputs.prompt.version,
                "sha256": inputs.prompt.sha256,
            },
            "bindings": [
                {
                    "figure_order": item.figure_order,
                    "number": item.editorial.number,
                    "page_key": item.page_key,
                    "chapter_id": item.chapter_id,
                    "document_page_number": item.document_page_number,
                    "eligible_page_number": item.eligible_page_number,
                    "page_unit_ids": list(item.page_unit_ids),
                }
                for item in bound
            ],
            "findings": [asdict(finding) for finding in findings],
        }

    def _validate_one(self, plan: VisualPlan) -> dict[str, Any]:
        if plan.disposition == "processing":
            raise IntegrityError("VISUAL_PLAN_INCOMPLETE", "Visual plan import is still processing")
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(plan.project_id)
            run = StageRunRepository(connection).get(plan.stage_run_id)
            if run.status is not RunStatus.DONE:
                raise IntegrityError(
                    "VISUAL_PLAN_RUN_INCOMPLETE", "Visual plan StageRun is not done"
                )
            if plan.raw_artifact_id is None or plan.validation_report_artifact_id is None:
                raise IntegrityError(
                    "VISUAL_PLAN_ARTIFACTS_INCOMPLETE",
                    "Completed visual plan has incomplete artifact evidence",
                )
            snapshot = PaginationRepository(connection).get(plan.pagination_snapshot_id)
            fields = (
                "project_id",
                "consolidation_id",
                "context_id",
                "production_set_hash",
                "text_artifact_id",
                "text_sha256",
                "consolidation_manifest_artifact_id",
                "consolidation_manifest_sha256",
            )
            if (
                any(getattr(snapshot, key) != getattr(plan, key) for key in fields)
                or snapshot.manifest_artifact_id != plan.pagination_manifest_artifact_id
                or snapshot.manifest_sha256 != plan.pagination_manifest_sha256
                or snapshot.eligible_page_count != plan.expected_figure_count
            ):
                raise IntegrityError(
                    "VISUAL_PLAN_PROVENANCE_INVALID",
                    "Visual plan provenance differs from its pagination snapshot",
                )
            for artifact_id, kind in (
                (plan.raw_artifact_id, "visual_plan_raw"),
                (plan.validation_report_artifact_id, "visual_plan_validation_report"),
            ):
                artifact = connection.execute(
                    "SELECT stage_run_id, artifact_type FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact is None or tuple(artifact) != (run.id, kind):
                    raise IntegrityError(
                        "VISUAL_PLAN_ARTIFACT_OWNER_INVALID",
                        "Visual plan artifact producer or type differs",
                    )
            expected_input = canonical_hash(
                {
                    "operation": "visual_plan_import@1",
                    "pagination_snapshot_id": plan.pagination_snapshot_id,
                    "pagination_manifest_sha256": plan.pagination_manifest_sha256,
                    "project_id": plan.project_id,
                    "prompt_id": plan.prompt_id,
                    "prompt_sha256": plan.prompt_sha256,
                    "prompt_version": plan.prompt_version,
                    "raw_sha256": plan.raw_sha256,
                    "text_sha256": plan.text_sha256,
                }
            )
            if (
                plan.input_hash != expected_input
                or run.input_hash != expected_input
                or run.stage_id != VISUAL_PLAN_STAGE
                or run.unit_id != VISUAL_PLAN_UNIT
                or run.project_id != plan.project_id
                or run.version != plan.raw_version
            ):
                raise IntegrityError("VISUAL_PLAN_INPUT_INVALID", "Visual plan identity differs")
            raw = self.persistence.read_artifact(
                connection, project, plan.raw_artifact_id, expected_sha256=plan.raw_sha256
            )
            report_bytes = self.persistence.read_artifact(
                connection,
                project,
                plan.validation_report_artifact_id,
                expected_sha256=plan.validation_report_sha256,
            )
            figures = VisualFigureRepository(connection).list(plan.id)
        _, manifest = self.pagination.show(plan.project_id, snapshot.version)
        prompt = self.prompts.resolve(plan.prompt_id, plan.prompt_version)
        if prompt.sha256 != plan.prompt_sha256:
            raise IntegrityError(
                "VISUAL_PLAN_PROMPT_PROVENANCE_INVALID",
                "Visual plan prompt differs from its immutable registered snapshot",
            )
        # Also checks the project's frozen snapshot, including its bytes and registered type.
        self.show(plan.project_id, plan.raw_version)
        parsed = parse_visual_plan(raw)
        bound: tuple[BoundVisualFigure, ...] = ()
        findings = parsed.findings
        if not findings:
            bound, findings = bind_figures_to_pagination(parsed.figures, manifest)
        expected_disposition = "valid" if not findings else "invalid"
        if (
            plan.disposition != expected_disposition
            or plan.figure_count != parsed.observed_figure_count
        ):
            raise IntegrityError(
                "VISUAL_PLAN_VALIDATION_STATE_INVALID",
                "Stored visual plan state differs from deterministic validation",
            )
        if plan.disposition == "valid":
            expected = self._materialize_figures(plan, bound)
            if len(figures) != plan.expected_figure_count:
                raise IntegrityError(
                    "VISUAL_PLAN_FIGURE_COUNT_INVALID",
                    "Materialized figure count differs from eligible_page_count",
                )
            for actual, wanted in zip(figures, expected, strict=True):
                actual_values = asdict(actual)
                wanted_values = asdict(wanted)
                actual_values.pop("id")
                wanted_values.pop("id")
                if actual_values != wanted_values:
                    raise IntegrityError(
                        "VISUAL_FIGURE_BINDING_INVALID",
                        "Materialized figure differs from deterministic page binding",
                    )
        elif figures:
            raise IntegrityError(
                "VISUAL_PLAN_INVALID_HAS_FIGURES",
                "Invalid visual plan must not materialize partial figures",
            )
        inputs = _VisualPlanInputs(project, snapshot, manifest, prompt)
        expected_report = (
            canonical_json_bytes(
                self._report(plan, inputs, parsed.observed_figure_count, bound, findings)
            )
            + b"\n"
        )
        if report_bytes != expected_report:
            raise IntegrityError(
                "VISUAL_PLAN_REPORT_MISMATCH",
                "Validation report differs from deterministic validation",
            )
        loaded = json.loads(report_bytes)
        if not isinstance(loaded, dict):
            raise IntegrityError(
                "VISUAL_PLAN_REPORT_INVALID", "Visual plan validation report is not an object"
            )
        return loaded

    def _assert_sources_unchanged(self, connection: sqlite3.Connection, plan: VisualPlan) -> None:
        latest_snapshot = PaginationRepository(connection).latest(plan.project_id)
        latest_consolidation = ConsolidationRepository(connection).latest(plan.project_id)
        if latest_snapshot is None or latest_snapshot.id != plan.pagination_snapshot_id:
            raise ConflictError(
                "VISUAL_PLAN_PAGINATION_CHANGED_DURING_IMPORT",
                "Canonical pagination changed before visual plan finalization",
            )
        if latest_consolidation is None or latest_consolidation.id != plan.consolidation_id:
            raise ConflictError(
                "VISUAL_PLAN_CONSOLIDATION_CHANGED_DURING_IMPORT",
                "Text consolidation changed before visual plan finalization",
            )
        if not self.pagination.status(plan.project_id).get("current", False):
            raise ConflictError("VISUAL_PLAN_SOURCE_STALE", "Text or pagination became stale")

    def require_plan(
        self, project_id: str, version: int | None = None, *, current: bool = True
    ) -> tuple[VisualPlan, list[VisualFigure], dict[str, Any], str]:
        plan, figures, _, _ = self.show(project_id, version)
        self._validate_one(plan)
        if plan.disposition != "valid":
            raise ConflictError("VISUAL_PLAN_INVALID", "Visual plan has structural errors")
        with self.database.connection() as connection:
            if current:
                self.assert_current(connection, plan)
            snapshot = PaginationRepository(connection).get(plan.pagination_snapshot_id)
            project = ProjectRepository(connection).get(project_id)
            text = self.persistence.read_artifact(
                connection, project, plan.text_artifact_id, expected_sha256=plan.text_sha256
            ).decode("utf-8")
        # The JSON manifest is operationally canonical. Its PDF evidence is still
        # hash/size/A4 checked; full PDF text extraction belongs to pagination validate.
        self.pagination.validate_snapshot(snapshot.id, verify_pdf_text=False)
        _, manifest = self.pagination.show(project_id, snapshot.version)
        return plan, figures, manifest, text

    def assert_current(self, connection: sqlite3.Connection, plan: VisualPlan) -> None:
        latest = VisualPlanRepository(connection).latest(plan.project_id)
        if latest is None or latest.id != plan.id:
            raise ConflictError("VISUAL_PLAN_NOT_CURRENT", "Visual plan has been superseded")
        self._assert_sources_unchanged(connection, plan)

    def _record_retry(self, plan: VisualPlan, exc: Exception) -> None:
        with self.database.connection() as connection, self.database.transaction(connection):
            runs = StageRunRepository(connection)
            run = runs.get(plan.stage_run_id)
            if run.status is not RunStatus.RUNNING:
                return
            code = exc.code if isinstance(exc, HeliosError) else "VISUAL_PLAN_UNEXPECTED_ERROR"
            message = exc.message if isinstance(exc, HeliosError) else str(exc)
            ErrorRepository(connection).add(
                project_id=plan.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=True,
            )
            retry = self.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
            runs.update(retry, expected_status=run.status)

    def _ensure_prompt_artifact(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        prompt: ResolvedPrompt,
        stored: StoredFile,
    ) -> None:
        artifacts = ArtifactRepository(connection)
        existing = artifacts.get_by_path(project.id, stored.relative_path)
        if existing is not None:
            if (
                existing.artifact_type != "prompt_snapshot"
                or existing.sha256 != prompt.sha256
                or existing.byte_size != len(prompt.content)
            ):
                raise ConflictError(
                    "VISUAL_PLAN_PROMPT_ARTIFACT_CONFLICT",
                    "Frozen visual-planning prompt metadata differs",
                )
            return
        self.persistence.register_artifact(
            connection,
            project,
            run,
            "prompt_snapshot",
            stored,
            prompt.version,
        )

    def _checkpoint(self, name: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name)

    def checkpoint(self, name: str) -> None:
        self._checkpoint(name)

    @staticmethod
    def _validate_json_file(path: Path) -> None:
        json.loads(path.read_bytes())
