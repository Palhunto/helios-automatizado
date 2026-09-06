from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from ebook_pipeline.core.errors import ConflictError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Project, RunStatus
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.visual_planning.anchor_repository import AnchorRepository
from ebook_pipeline.visual_planning.anchors import candidate_report
from ebook_pipeline.visual_planning.models import (
    VisualAnchor,
    VisualFigure,
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

ANCHOR_PROMPT = "helios_visual_anchor_enrichment"
ANCHOR_RULE = "literal_page_unit@1"
ANCHOR_STAGE = "visual_anchor_enrichment"


def anchor_identity(plan: VisualPlan, figure: VisualFigure, raw_sha: str, prompt_sha: str) -> str:
    return canonical_hash(
        {
            "operation": "visual_anchor_import@1",
            "plan_input_hash": plan.input_hash,
            "project_id": plan.project_id,
            "plan_id": plan.id,
            "figure_id": figure.id,
            "editorial_sha256": figure.editorial_sha256,
            "raw_sha256": raw_sha,
            "prompt_id": ANCHOR_PROMPT,
            "prompt_version": 1,
            "prompt_sha256": prompt_sha,
            "validation_rule_version": ANCHOR_RULE,
        }
    )


class VisualAnchorService:
    def __init__(self, visual: VisualPlanningService) -> None:
        self.visual = visual

    def request(self, project_id: str, figure_id: str) -> dict[str, Any]:
        plan, figure, page, text = self._sources(project_id, figure_id, current=True)
        prompt = self.visual.prompts.resolve(ANCHOR_PROMPT, 1)
        return {
            "prompt": {"id": prompt.id, "version": prompt.version, "sha256": prompt.sha256},
            "visual_plan_id": plan.id,
            "figure_id": figure.id,
            "page_key": figure.page_key,
            "request": prompt.content.decode("utf-8")
            + "\n"
            + json.dumps(
                {
                    "figure": asdict(figure),
                    "page_units": [
                        {
                            "unit_id": span["unit_id"],
                            "text": text[span["global_char_start"] : span["global_char_end"]],
                        }
                        for span in page["unit_spans"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        }

    def import_raw(
        self, project_id: str, figure_id: str, raw: bytes
    ) -> tuple[VisualAnchor, dict[str, Any]]:
        visual = self.visual
        plan, figure, page, text = self._sources(project_id, figure_id, current=True)
        prompt = visual.prompts.resolve(ANCHOR_PROMPT, 1)
        raw_sha = sha256_bytes(raw)
        identity = anchor_identity(plan, figure, raw_sha, prompt.sha256)
        with visual.database.connection() as connection, visual.database.transaction(connection):
            visual.assert_current(connection, plan)
            repository = AnchorRepository(connection)
            anchor = repository.by_input(figure_id, identity)
            if anchor is None:
                latest = repository.latest(figure_id)
                version = 1 if latest is None else latest.version + 1
                run = visual.persistence.new_run(
                    project_id=project_id,
                    stage_id=ANCHOR_STAGE,
                    unit_id=figure_id,
                    input_hash=identity,
                    version=version,
                    supersedes_run_id=None if latest is None else latest.stage_run_id,
                )
                visual.persistence.add_and_start(connection, run)
                anchor = VisualAnchor(
                    id=new_id(),
                    project_id=project_id,
                    visual_plan_id=plan.id,
                    figure_id=figure_id,
                    stage_run_id=run.id,
                    version=version,
                    input_hash=identity,
                    supersedes_anchor_id=None if latest is None else latest.id,
                    disposition="processing",
                    prompt_id=prompt.id,
                    prompt_version=prompt.version,
                    prompt_sha256=prompt.sha256,
                    raw_sha256=raw_sha,
                    raw_artifact_id=None,
                    report_sha256=None,
                    report_artifact_id=None,
                    unit_id=None,
                    start_offset=None,
                    end_offset=None,
                    created_at=utc_now(),
                    validated_at=None,
                )
                repository.add(anchor)
            run = resume(visual, connection, anchor.stage_run_id)
            if (run.status is RunStatus.DONE) != (anchor.disposition != "processing"):
                raise IntegrityError("VISUAL_ANCHOR_STATE_INVALID", "Anchor/run states disagree")
        if run.status is RunStatus.DONE:
            return anchor, self.validate_one(anchor)
        try:
            project = self._project(project_id)
            base = f"visual-plan/anchors/{figure_id}/v{anchor.version:04d}"
            source = visual.store.write_bytes(project.artifact_root, f"{base}/raw.json", raw)
            visual.checkpoint("visual_anchor.raw")
            prompt_path = f"prompts/frozen/{prompt.id}/v{prompt.version:04d}.txt"
            frozen = visual.store.write_bytes(project.artifact_root, prompt_path, prompt.content)
            report = self._report(anchor, plan, figure, raw, page, text)
            stored = visual.store.write_bytes(
                project.artifact_root,
                f"{base}/validation.json",
                canonical_json_bytes(report) + b"\n",
            )
            visual.checkpoint("visual_anchor.validation")
            with (
                visual.database.connection() as connection,
                visual.database.transaction(connection),
            ):
                visual.assert_current(connection, plan)
                current = AnchorRepository(connection).get(anchor.id)
                run = StageRunRepository(connection).get(anchor.stage_run_id)
                if current.disposition != "processing" or run.status is not RunStatus.RUNNING:
                    raise ConflictError("VISUAL_ANCHOR_CONCURRENT_UPDATE", "Anchor already changed")
                refreshed_plan, refreshed_figure, refreshed_page, refreshed_text = self._sources(
                    project_id, figure_id, current=False
                )
                refreshed = self._report(
                    anchor, refreshed_plan, refreshed_figure, raw, refreshed_page, refreshed_text
                )
                if refreshed != report:
                    raise IntegrityError("VISUAL_ANCHOR_SOURCE_CHANGED", "Anchor sources changed")
                verify_files(visual, project, source, stored, frozen)
                source_art = visual.persistence.register_artifact(
                    connection, project, run, "visual_anchor_raw", source, anchor.version
                )
                report_art = visual.persistence.register_artifact(
                    connection, project, run, "visual_anchor_validation", stored, anchor.version
                )
                existing = ArtifactRepository(connection).get_by_path(project_id, prompt_path)
                if existing is None:
                    visual.persistence.register_artifact(
                        connection, project, run, "prompt_snapshot", frozen, prompt.version
                    )
                elif (
                    existing.sha256 != prompt.sha256 or existing.artifact_type != "prompt_snapshot"
                ):
                    raise IntegrityError("VISUAL_ANCHOR_PROMPT_INVALID", "Frozen prompt differs")
                result = report["validation"]
                anchor = replace(
                    anchor,
                    disposition="valid" if result["valid"] else "invalid",
                    raw_artifact_id=source_art.id,
                    report_artifact_id=report_art.id,
                    report_sha256=report_art.sha256,
                    unit_id=result["unit_id"],
                    start_offset=result["start_offset"],
                    end_offset=result["end_offset"],
                    validated_at=utc_now(),
                )
                AnchorRepository(connection).complete(anchor)
                visual.persistence.finish(connection, run.id)
            log_result(run, anchor.disposition)
            return anchor, report
        except Exception as exc:
            record_failure(visual, anchor.stage_run_id, exc)
            raise

    def show(
        self, project_id: str, figure_id: str, version: int | None = None
    ) -> tuple[VisualAnchor, dict[str, Any]]:
        self.visual.show_figure(project_id, figure_id)
        with self.visual.database.connection() as connection:
            candidates = AnchorRepository(connection).list(figure_id)
        if version is not None:
            candidates = [a for a in candidates if a.version == version]
        if not candidates:
            raise NotFoundError("VISUAL_ANCHOR_NOT_FOUND", "No anchor for this figure/version")
        anchor = candidates[-1]
        return anchor, self.validate_one(anchor)

    def validate_one(
        self, anchor: VisualAnchor, *, sources: VisualPlanSources | None = None
    ) -> dict[str, Any]:
        visual = self.visual
        if sources is None:
            plan, figure, page, text = self._sources(
                anchor.project_id, anchor.figure_id, current=False
            )
        else:
            plan, figures, pagination, text = sources
            matches = [f for f in figures if f.id == anchor.figure_id]
            if len(matches) != 1 or anchor.project_id != plan.project_id:
                raise IntegrityError(
                    "VISUAL_ANCHOR_SOURCE_INVALID", "Anchor source binding differs"
                )
            figure = matches[0]
            page = next(p for p in pagination["pages"] if p["page_key"] == figure.page_key)
        with visual.database.connection() as connection:
            run = StageRunRepository(connection).get(anchor.stage_run_id)
            if run.status is not RunStatus.DONE or anchor.disposition == "processing":
                raise IntegrityError("VISUAL_ANCHOR_INCOMPLETE", "Anchor requires recovery")
            raw = read_owned(
                visual,
                connection,
                run,
                anchor.raw_artifact_id,
                anchor.raw_sha256,
                "visual_anchor_raw",
            )
            report_bytes = read_owned(
                visual,
                connection,
                run,
                anchor.report_artifact_id,
                anchor.report_sha256,
                "visual_anchor_validation",
            )
            prompt = visual.prompts.resolve(ANCHOR_PROMPT, 1)
            frozen = ArtifactRepository(connection).get_by_path(
                anchor.project_id, f"prompts/frozen/{prompt.id}/v0001.txt"
            )
            if frozen is None or frozen.artifact_type != "prompt_snapshot":
                raise IntegrityError("VISUAL_ANCHOR_PROMPT_INVALID", "Frozen prompt missing")
            visual.persistence.read_artifact(
                connection,
                ProjectRepository(connection).get(anchor.project_id),
                frozen.id,
                expected_sha256=prompt.sha256,
            )
        expected_identity = anchor_identity(plan, figure, anchor.raw_sha256, prompt.sha256)
        if (
            anchor.input_hash != expected_identity
            or run.input_hash != expected_identity
            or anchor.visual_plan_id != plan.id
            or anchor.prompt_id != prompt.id
            or anchor.prompt_version != 1
            or anchor.prompt_sha256 != prompt.sha256
            or run.project_id != anchor.project_id
            or run.stage_id != ANCHOR_STAGE
            or run.unit_id != figure.id
            or run.version != anchor.version
        ):
            raise IntegrityError("VISUAL_ANCHOR_PROVENANCE_INVALID", "Anchor identity differs")
        report = self._report(anchor, plan, figure, raw, page, text)
        result = report["validation"]
        if (
            report_bytes != canonical_json_bytes(report) + b"\n"
            or (anchor.disposition == "valid") != result["valid"]
            or anchor.unit_id != result["unit_id"]
            or anchor.start_offset != result["start_offset"]
            or anchor.end_offset != result["end_offset"]
        ):
            raise IntegrityError("VISUAL_ANCHOR_REPORT_MISMATCH", "Anchor validation differs")
        return report

    def _sources(
        self, project_id: str, figure_id: str, *, current: bool
    ) -> tuple[VisualPlan, VisualFigure, dict[str, Any], str]:
        figure = self.visual.show_figure(project_id, figure_id)
        with self.visual.database.connection() as connection:
            plan = VisualPlanRepository(connection).get(figure.visual_plan_id)
        plan, _, manifest, text = self.visual.require_plan(
            project_id, plan.raw_version, current=current
        )
        page = next(p for p in manifest["pages"] if p["page_key"] == figure.page_key)
        return plan, figure, page, text

    def _project(self, project_id: str) -> Project:
        with self.visual.database.connection() as connection:
            return ProjectRepository(connection).get(project_id)

    def recover(self, project_id: str, figure_id: str) -> tuple[VisualAnchor, dict[str, Any]]:
        self.visual.show_figure(project_id, figure_id)
        with self.visual.database.connection() as connection:
            anchor = AnchorRepository(connection).latest(figure_id)
        if anchor is None:
            raise NotFoundError("VISUAL_ANCHOR_NOT_FOUND", "No anchor to recover")
        if anchor.disposition != "processing":
            return anchor, self.validate_one(anchor)
        path = f"visual-plan/anchors/{figure_id}/v{anchor.version:04d}/raw.json"
        project = self._project(project_id)
        stored = self.visual.store.inspect(project.artifact_root, path)
        if stored.sha256 != anchor.raw_sha256:
            raise IntegrityError("VISUAL_ANCHOR_RAW_MISMATCH", "Recovery source hash differs")
        raw = self.visual.store.resolve(project.artifact_root, path).read_bytes()
        return self.import_raw(project_id, figure_id, raw)

    @staticmethod
    def _report(
        anchor: VisualAnchor,
        plan: VisualPlan,
        figure: VisualFigure,
        raw: bytes,
        page: dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        return {
            "schema": "helios_visual_anchor_validation@1",
            "visual_anchor_id": anchor.id,
            "version": anchor.version,
            "figure_id": figure.id,
            "visual_plan_id": plan.id,
            "project_id": plan.project_id,
            "page_key": figure.page_key,
            "input_hash": anchor.input_hash,
            "raw_sha256": anchor.raw_sha256,
            "validation_rule_version": ANCHOR_RULE,
            "pagination_snapshot_id": plan.pagination_snapshot_id,
            "pagination_manifest_sha256": plan.pagination_manifest_sha256,
            "consolidation_id": plan.consolidation_id,
            "text_sha256": plan.text_sha256,
            "prompt": {
                "id": anchor.prompt_id,
                "version": anchor.prompt_version,
                "sha256": anchor.prompt_sha256,
            },
            **candidate_report(raw, page, text),
        }
