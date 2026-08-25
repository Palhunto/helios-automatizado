from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Project, RunStatus, ValidationIssue
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.pagination.contracts import (
    PaginationLayoutRegistry,
    ResolvedPaginationLayout,
)
from ebook_pipeline.pagination.models import (
    ConsolidationMemberSource,
    RenderedPagination,
    RendererFingerprint,
    SourceLedger,
    VisualPaginationSnapshot,
)
from ebook_pipeline.pagination.recovery import PaginationRecovery
from ebook_pipeline.pagination.renderer import ChromiumPaginationRenderer, validate_pdf
from ebook_pipeline.pagination.repositories import PaginationRepository
from ebook_pipeline.pagination.source_map import build_source_ledger
from ebook_pipeline.pagination.validators import validate_manifest_structure
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.models import TextConsolidation, WritingContext
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.repositories import (
    ConsolidationRepository,
    WritingArtifactRepository,
    WritingContextRepository,
)
from ebook_pipeline.writing.service import WritingService

PAGINATION_STAGE = "visual_pagination"
PAGINATION_UNIT = "paginate"


@dataclass(frozen=True, slots=True)
class _PaginationInputs:
    project: Project
    consolidation: TextConsolidation
    context: WritingContext
    ledger: SourceLedger


class PaginationService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        writing: WritingService,
        *,
        renderer: ChromiumPaginationRenderer | None = None,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.writing = writing
        self.renderer = renderer or ChromiumPaginationRenderer()
        self.persistence = WritingPersistence(config, database, store, fault_hook)
        self.recovery = PaginationRecovery()
        self.state_machine = StateMachine()
        self.fault_hook = fault_hook
        self.layouts = PaginationLayoutRegistry(config.pagination_layout_registry)

    def create(
        self,
        project_id: str,
        *,
        layout_id: str = "helios_pagination_layout",
        layout_version: int = 1,
    ) -> VisualPaginationSnapshot:
        layout = self.layouts.resolve(layout_id, layout_version)
        inputs = self._load_inputs(project_id, layout)
        renderer_fingerprint = self.renderer.fingerprint()
        input_hash = self._input_hash(inputs, layout, renderer_fingerprint)
        snapshot, run_done = self._reserve(inputs, layout, renderer_fingerprint, input_hash)
        if run_done:
            self._validate_one(snapshot)
            return snapshot
        try:
            rendered = self.renderer.render(
                inputs.ledger,
                layout,
                input_hash=input_hash,
                renderer=renderer_fingerprint,
            )
            self._validate_rendered(rendered, inputs.ledger)
            return self._persist(snapshot, inputs, layout, rendered)
        except Exception as exc:
            self._record_retry(snapshot, exc)
            raise

    def show(
        self, project_id: str, version: int | None = None
    ) -> tuple[VisualPaginationSnapshot, dict[str, Any]]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            repository = PaginationRepository(connection)
            snapshot = repository.latest(project_id)
            if version is not None:
                row = connection.execute(
                    "SELECT id FROM visual_pagination_snapshots "
                    "WHERE project_id = ? AND version = ?",
                    (project_id, version),
                ).fetchone()
                snapshot = None if row is None else repository.get(str(row["id"]))
            if snapshot is None or snapshot.manifest_artifact_id is None:
                raise NotFoundError(
                    "PAGINATION_SNAPSHOT_NOT_FOUND",
                    "No complete canonical pagination snapshot was found",
                )
            project = ProjectRepository(connection).get(project_id)
            manifest = self.persistence.read_artifact(
                connection,
                project,
                snapshot.manifest_artifact_id,
                expected_sha256=snapshot.manifest_sha256,
            )
            return snapshot, validate_manifest_structure(snapshot, manifest)

    def status(self, project_id: str) -> dict[str, Any]:
        fingerprint = self.renderer.fingerprint()
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            snapshot = PaginationRepository(connection).latest(project_id)
            consolidation = ConsolidationRepository(connection).latest(project_id)
            if snapshot is None:
                return {"project_id": project_id, "status": "missing"}
            run = StageRunRepository(connection).get(snapshot.stage_run_id)
        layout = self.layouts.resolve(snapshot.layout_id, snapshot.layout_version)
        production_set = self.writing.production_set(project_id)
        stale_reasons: list[str] = []
        if consolidation is None or snapshot.consolidation_id != consolidation.id:
            stale_reasons.append("consolidation_changed")
        elif snapshot.text_sha256 != consolidation.text_sha256:
            stale_reasons.append("consolidated_text_changed")
        if (
            not production_set.current
            or production_set.production_set_hash != snapshot.production_set_hash
        ):
            stale_reasons.append("text_production_set_changed")
        if snapshot.layout_sha256 != layout.sha256:
            stale_reasons.append("layout_changed")
        if snapshot.renderer_fingerprint != fingerprint.fingerprint:
            stale_reasons.append("renderer_fingerprint_changed")
        return {
            "project_id": project_id,
            "snapshot_id": snapshot.id,
            "version": snapshot.version,
            "stage_run_status": run.status.value,
            "current": run.status is RunStatus.DONE and not stale_reasons,
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "recovery_required": run.status is not RunStatus.DONE,
            "document_page_count": snapshot.document_page_count,
            "eligible_page_count": snapshot.eligible_page_count,
        }

    def validate(self, project_id: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            rows = connection.execute(
                "SELECT id FROM visual_pagination_snapshots WHERE project_id = ? ORDER BY version",
                (project_id,),
            ).fetchall()
        for row in rows:
            try:
                self._validate_one_by_id(str(row["id"]))
            except HeliosError as exc:
                issues.append(ValidationIssue(exc.code, exc.message))
        return issues

    def _load_inputs(
        self,
        project_id: str,
        layout: ResolvedPaginationLayout,
    ) -> _PaginationInputs:
        production_set = self.writing.production_set(project_id)
        if (
            not production_set.current
            or not production_set.complete
            or production_set.production_set_hash is None
        ):
            raise ConflictError(
                "PAGINATION_TEXT_PRODUCTION_SET_NOT_CURRENT",
                "Canonical pagination requires a complete current text production set",
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            consolidation = ConsolidationRepository(connection).latest(project_id)
            if consolidation is None:
                raise NotFoundError(
                    "TEXT_CONSOLIDATION_NOT_FOUND",
                    "Canonical pagination requires a text consolidation",
                )
            run = StageRunRepository(connection).get(consolidation.stage_run_id)
            if run.status is not RunStatus.DONE:
                raise IntegrityError(
                    "PAGINATION_CONSOLIDATION_NOT_ACCEPTED",
                    "Canonical pagination requires a completed text consolidation",
                )
            if (
                consolidation.context_id != production_set.context_id
                or consolidation.production_set_hash != production_set.production_set_hash
            ):
                raise ConflictError(
                    "PAGINATION_CONSOLIDATION_STALE",
                    "Latest text consolidation does not represent the current production set",
                )
            if consolidation.text_artifact_id is None or consolidation.manifest_artifact_id is None:
                raise IntegrityError(
                    "PAGINATION_CONSOLIDATION_ARTIFACTS_MISSING",
                    "Text consolidation artifacts are incomplete",
                )
            context = WritingContextRepository(connection).get(consolidation.context_id)
            contract = self.writing.contexts.resolve_contract(context)
            if (
                context.contract_id != contract.id
                or context.contract_version != contract.version
                or context.contract_sha256 != contract.sha256
            ):
                raise IntegrityError(
                    "PAGINATION_WRITING_CONTRACT_BINDING_INVALID",
                    "WritingContext contract provenance is inconsistent",
                )
            if [unit.unit_id for unit in contract.model.ordered_units()] != [
                unit.unit_id for unit in layout.model.ordered_units()
            ]:
                raise ConflictError(
                    "PAGINATION_LAYOUT_WRITING_CONTRACT_MISMATCH",
                    "Pagination layout units do not match the Writing Production Contract",
                )
            text = self.persistence.read_artifact(
                connection,
                project,
                consolidation.text_artifact_id,
                expected_sha256=consolidation.text_sha256,
            )
            manifest_bytes = self.persistence.read_artifact(
                connection,
                project,
                consolidation.manifest_artifact_id,
                expected_sha256=consolidation.manifest_sha256,
            )
            self._validate_consolidation_manifest(consolidation, context, manifest_bytes)
            members: list[ConsolidationMemberSource] = []
            artifacts = WritingArtifactRepository(connection)
            for row in ConsolidationRepository(connection).members(consolidation.id):
                artifact = artifacts.get(str(row["artifact_id"]))
                if artifact.artifact_type != "text_unit_accepted":
                    raise IntegrityError(
                        "PAGINATION_MEMBER_ARTIFACT_TYPE_INVALID",
                        "Consolidation member is not an accepted text artifact",
                    )
                content = self.persistence.read_artifact(
                    connection,
                    project,
                    artifact.id,
                    expected_sha256=str(row["sha256"]),
                )
                members.append(
                    ConsolidationMemberSource(
                        unit_id=str(row["unit_id"]),
                        unit_order=int(row["unit_order"]),
                        artifact_id=artifact.id,
                        sha256=str(row["sha256"]),
                        content=content,
                    )
                )
            ledger = build_source_ledger(
                consolidated_bytes=text,
                separator=consolidation.separator,
                members=tuple(members),
                layout=layout.model,
            )
            return _PaginationInputs(project, consolidation, context, ledger)

    def _reserve(
        self,
        inputs: _PaginationInputs,
        layout: ResolvedPaginationLayout,
        renderer: RendererFingerprint,
        input_hash: str,
    ) -> tuple[VisualPaginationSnapshot, bool]:
        with self.database.connection() as connection, self.database.transaction(connection):
            repository = PaginationRepository(connection)
            existing = repository.by_input_hash(inputs.project.id, input_hash)
            if existing is not None:
                run = self.recovery.resume(connection, existing)
                return existing, run.status is RunStatus.DONE
            latest = repository.latest(inputs.project.id)
            version = 1 if latest is None else latest.version + 1
            run = self.persistence.new_run(
                project_id=inputs.project.id,
                stage_id=PAGINATION_STAGE,
                unit_id=PAGINATION_UNIT,
                input_hash=input_hash,
                version=version,
                supersedes_run_id=None if latest is None else latest.stage_run_id,
            )
            running = self.persistence.add_and_start(connection, run)
            consolidation = inputs.consolidation
            assert consolidation.text_artifact_id is not None
            assert consolidation.manifest_artifact_id is not None
            snapshot = VisualPaginationSnapshot(
                id=new_id(),
                project_id=inputs.project.id,
                stage_run_id=running.id,
                version=version,
                input_hash=input_hash,
                consolidation_id=consolidation.id,
                context_id=consolidation.context_id,
                production_set_hash=consolidation.production_set_hash,
                text_artifact_id=consolidation.text_artifact_id,
                text_sha256=consolidation.text_sha256,
                consolidation_manifest_artifact_id=consolidation.manifest_artifact_id,
                consolidation_manifest_sha256=consolidation.manifest_sha256,
                writing_contract_id=inputs.context.contract_id,
                writing_contract_version=inputs.context.contract_version,
                writing_contract_sha256=inputs.context.contract_sha256,
                layout_id=layout.id,
                layout_version=layout.version,
                layout_sha256=layout.sha256,
                renderer_fingerprint=renderer.fingerprint,
                html_sha256=None,
                pdf_sha256=None,
                manifest_sha256=None,
                html_artifact_id=None,
                pdf_artifact_id=None,
                manifest_artifact_id=None,
                document_page_count=0,
                eligible_page_count=0,
                created_at=utc_now(),
            )
            repository.add(snapshot)
            return snapshot, False

    def _persist(
        self,
        snapshot: VisualPaginationSnapshot,
        inputs: _PaginationInputs,
        layout: ResolvedPaginationLayout,
        rendered: RenderedPagination,
    ) -> VisualPaginationSnapshot:
        base = f"visual-plan/pagination/v{snapshot.version:04d}"
        html_path = f"{base}/render.html"
        pdf_path = f"{base}/document.pdf"
        manifest_path = f"{base}/manifest.json"
        html_sha256 = sha256_bytes(rendered.html)
        pdf_sha256 = sha256_bytes(rendered.pdf)
        manifest = self._manifest(
            snapshot,
            inputs,
            layout,
            rendered,
            html_path=html_path,
            html_sha256=html_sha256,
            pdf_path=pdf_path,
            pdf_sha256=pdf_sha256,
        )
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        manifest_sha256 = sha256_bytes(manifest_bytes)
        html_stored = self.store.write_bytes(
            inputs.project.artifact_root,
            html_path,
            rendered.html,
            validator=self._validate_utf8_file,
        )
        self._checkpoint("pagination.html")
        pdf_stored = self.store.write_bytes(
            inputs.project.artifact_root,
            pdf_path,
            rendered.pdf,
            validator=lambda path: validate_pdf(path.read_bytes(), len(rendered.pages)),
        )
        self._checkpoint("pagination.pdf")
        manifest_stored = self.store.write_bytes(
            inputs.project.artifact_root,
            manifest_path,
            manifest_bytes,
            validator=self._validate_json_file,
        )
        self._checkpoint("pagination.manifest")
        with self.database.connection() as connection, self.database.transaction(connection):
            latest = ConsolidationRepository(connection).latest(inputs.project.id)
            if latest is None or latest.id != snapshot.consolidation_id:
                raise ConflictError(
                    "PAGINATION_CONSOLIDATION_CHANGED_DURING_RENDER",
                    "Text consolidation changed before pagination could be finalized",
                )
            repository = PaginationRepository(connection)
            current = repository.get(snapshot.id)
            run = StageRunRepository(connection).get(snapshot.stage_run_id)
            html_artifact = self.persistence.register_artifact(
                connection,
                inputs.project,
                run,
                "pagination_render_html",
                html_stored,
                snapshot.version,
            )
            pdf_artifact = self.persistence.register_artifact(
                connection,
                inputs.project,
                run,
                "pagination_pdf",
                pdf_stored,
                snapshot.version,
            )
            manifest_artifact = self.persistence.register_artifact(
                connection,
                inputs.project,
                run,
                "pagination_snapshot_manifest",
                manifest_stored,
                snapshot.version,
            )
            for page in rendered.pages:
                repository.add_page(current, page)
            completed = repository.set_complete(
                current,
                html_artifact_id=html_artifact.id,
                html_sha256=html_sha256,
                pdf_artifact_id=pdf_artifact.id,
                pdf_sha256=pdf_sha256,
                manifest_artifact_id=manifest_artifact.id,
                manifest_sha256=manifest_sha256,
                document_page_count=len(rendered.pages),
                eligible_page_count=sum(page.eligible for page in rendered.pages),
            )
            self.persistence.finish(connection, run.id)
        self._validate_one(completed)
        return completed

    def _manifest(
        self,
        snapshot: VisualPaginationSnapshot,
        inputs: _PaginationInputs,
        layout: ResolvedPaginationLayout,
        rendered: RenderedPagination,
        *,
        html_path: str,
        html_sha256: str,
        pdf_path: str,
        pdf_sha256: str,
    ) -> dict[str, Any]:
        model = layout.model
        pages = []
        for page in rendered.pages:
            text_region = model.page.eligible_text if page.eligible else model.page.noneligible_text
            pages.append(
                {
                    "chapter_id": page.chapter_id,
                    "chapter_page_number": page.chapter_page_number,
                    "document_page_number": page.document_page_number,
                    "eligible": page.eligible,
                    "eligible_page_number": page.eligible_page_number,
                    "page_key": page.page_key,
                    "page_source_sha256": page.page_source_sha256,
                    "text_region": text_region.model_dump(mode="json"),
                    "unit_spans": [asdict(span) for span in page.unit_spans],
                    "visual_slot": (
                        model.page.visual_slot.model_dump(mode="json") if page.eligible else None
                    ),
                }
            )
        consolidation = inputs.consolidation
        assert consolidation.text_artifact_id is not None
        assert consolidation.manifest_artifact_id is not None
        return {
            "schema": "helios_pagination_snapshot@1",
            "snapshot_id": snapshot.id,
            "project_id": snapshot.project_id,
            "version": snapshot.version,
            "input_hash": snapshot.input_hash,
            "created_at": snapshot.created_at,
            "consolidation": {
                "id": consolidation.id,
                "version": consolidation.version,
                "context_id": consolidation.context_id,
                "production_set_hash": consolidation.production_set_hash,
                "text_artifact_id": consolidation.text_artifact_id,
                "text_sha256": consolidation.text_sha256,
                "manifest_artifact_id": consolidation.manifest_artifact_id,
                "manifest_sha256": consolidation.manifest_sha256,
                "writing_contract": {
                    "id": inputs.context.contract_id,
                    "version": inputs.context.contract_version,
                    "sha256": inputs.context.contract_sha256,
                },
            },
            "layout": {
                "id": layout.id,
                "version": layout.version,
                "sha256": layout.sha256,
                "chapter_boundary": model.chapter_boundary.model_dump(mode="json"),
                "fonts": [
                    {
                        "family": font.contract.family,
                        "font_sha256": font.contract.sha256,
                        "license_sha256": font.contract.license_sha256,
                        "role": font.contract.role,
                    }
                    for font in layout.fonts
                ],
            },
            "renderer": {
                **asdict(rendered.renderer),
                "engine": model.renderer.engine,
                "paginator_version": model.renderer.paginator_version,
                "template_version": model.renderer.template_version,
                "pdf_canonicalization_version": model.renderer.pdf_canonicalization_version,
            },
            "coordinate_system": {
                "character_offsets": "unicode_code_points",
                "byte_offsets": "utf8_bytes",
                "intervals": "half_open",
                "source": "exact_text_consolidation",
            },
            "artifacts": {
                "html": {
                    "artifact_type": "pagination_render_html",
                    "relative_path": html_path,
                    "sha256": html_sha256,
                },
                "pdf": {
                    "artifact_type": "pagination_pdf",
                    "relative_path": pdf_path,
                    "sha256": pdf_sha256,
                },
            },
            "document_page_count": len(rendered.pages),
            "eligible_page_count": sum(page.eligible for page in rendered.pages),
            "pages": pages,
        }

    def _input_hash(
        self,
        inputs: _PaginationInputs,
        layout: ResolvedPaginationLayout,
        renderer: RendererFingerprint,
    ) -> str:
        return canonical_hash(
            {
                "schema": "helios_pagination_snapshot@1",
                "project_id": inputs.project.id,
                "consolidation_id": inputs.consolidation.id,
                "production_set_hash": inputs.consolidation.production_set_hash,
                "text_artifact_id": inputs.consolidation.text_artifact_id,
                "text_sha256": inputs.consolidation.text_sha256,
                "consolidation_manifest_artifact_id": (
                    inputs.consolidation.manifest_artifact_id
                ),
                "consolidation_manifest_sha256": inputs.consolidation.manifest_sha256,
                "writing_contract": {
                    "id": inputs.context.contract_id,
                    "version": inputs.context.contract_version,
                    "sha256": inputs.context.contract_sha256,
                },
                "layout": {
                    "id": layout.id,
                    "version": layout.version,
                    "sha256": layout.sha256,
                    "font_hashes": [font.contract.sha256 for font in layout.fonts],
                    "license_hashes": [font.contract.license_sha256 for font in layout.fonts],
                    "renderer_contract": layout.model.renderer.model_dump(mode="json"),
                },
                "renderer": asdict(renderer),
            }
        )

    def _validate_one_by_id(self, snapshot_id: str) -> None:
        with self.database.connection() as connection:
            snapshot = PaginationRepository(connection).get(snapshot_id)
        self._validate_one(snapshot)

    def _validate_one(self, snapshot: VisualPaginationSnapshot) -> None:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(snapshot.project_id)
            run = StageRunRepository(connection).get(snapshot.stage_run_id)
            if run.status is not RunStatus.DONE:
                raise IntegrityError(
                    "PAGINATION_RUN_INCOMPLETE", "Pagination snapshot StageRun is not done"
                )
            if (
                snapshot.html_artifact_id is None
                or snapshot.pdf_artifact_id is None
                or snapshot.manifest_artifact_id is None
                or snapshot.html_sha256 is None
                or snapshot.pdf_sha256 is None
                or snapshot.manifest_sha256 is None
            ):
                raise IntegrityError(
                    "PAGINATION_ARTIFACTS_INCOMPLETE",
                    "Completed pagination snapshot has incomplete artifact evidence",
                )
            html_bytes = self.persistence.read_artifact(
                connection,
                project,
                snapshot.html_artifact_id,
                expected_sha256=snapshot.html_sha256,
            )
            pdf_bytes = self.persistence.read_artifact(
                connection,
                project,
                snapshot.pdf_artifact_id,
                expected_sha256=snapshot.pdf_sha256,
            )
            manifest_bytes = self.persistence.read_artifact(
                connection,
                project,
                snapshot.manifest_artifact_id,
                expected_sha256=snapshot.manifest_sha256,
            )
            manifest = validate_manifest_structure(snapshot, manifest_bytes)
            repository = PaginationRepository(connection)
            pages = repository.page_rows(snapshot.id)
            spans = repository.span_rows(snapshot.id)
            if len(pages) != snapshot.document_page_count:
                raise IntegrityError(
                    "PAGINATION_PAGE_LEDGER_COUNT_INVALID",
                    "SQLite page ledger differs from document_page_count",
                )
            if len([row for row in pages if bool(row["eligible"])]) != snapshot.eligible_page_count:
                raise IntegrityError(
                    "PAGINATION_ELIGIBLE_LEDGER_COUNT_INVALID",
                    "SQLite eligible page ledger differs from eligible_page_count",
                )
            manifest_spans = sum(len(page["unit_spans"]) for page in manifest["pages"])
            if len(spans) != manifest_spans:
                raise IntegrityError(
                    "PAGINATION_SPAN_LEDGER_COUNT_INVALID",
                    "SQLite and manifest unit span counts differ",
                )
            consolidation_text = self.persistence.read_artifact(
                connection,
                project,
                snapshot.text_artifact_id,
                expected_sha256=snapshot.text_sha256,
            )
            try:
                consolidation_decoded = consolidation_text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IntegrityError(
                    "PAGINATION_CONSOLIDATION_UTF8_INVALID",
                    "Pagination source consolidation is no longer valid UTF-8",
                ) from exc
            artifacts = WritingArtifactRepository(connection)
            spans_by_page: dict[int, list[Any]] = {}
            for span in spans:
                spans_by_page.setdefault(int(span["document_page_number"]), []).append(span)
            for row, manifest_page in zip(pages, manifest["pages"], strict=True):
                self._validate_page_ledger(row, manifest_page)
                document_number = int(row["document_page_number"])
                source_chunks: list[str] = []
                manifest_page_spans = manifest_page["unit_spans"]
                for span_row, manifest_span in zip(
                    spans_by_page.get(document_number, []),
                    manifest_page_spans,
                    strict=True,
                ):
                    self._validate_span_ledger(span_row, manifest_span)
                    global_char_start = int(span_row["global_char_start"])
                    global_char_end = int(span_row["global_char_end"])
                    global_byte_start = int(span_row["global_byte_start"])
                    global_byte_end = int(span_row["global_byte_end"])
                    global_text = consolidation_decoded[global_char_start:global_char_end]
                    if consolidation_text[global_byte_start:global_byte_end] != global_text.encode(
                        "utf-8"
                    ):
                        raise IntegrityError(
                            "PAGINATION_GLOBAL_OFFSETS_INVALID",
                            "Character and UTF-8 byte offsets are not reversible",
                        )
                    source_artifact = artifacts.get(str(span_row["artifact_id"]))
                    source = self.persistence.read_artifact(
                        connection,
                        project,
                        source_artifact.id,
                        expected_sha256=str(span_row["source_sha256"]),
                    )
                    source_text = source.decode("utf-8")
                    unit_char_start = int(span_row["unit_char_start"])
                    unit_char_end = int(span_row["unit_char_end"])
                    unit_byte_start = int(span_row["unit_byte_start"])
                    unit_byte_end = int(span_row["unit_byte_end"])
                    if source_text[unit_char_start:unit_char_end] != global_text:
                        raise IntegrityError(
                            "PAGINATION_UNIT_OFFSETS_INVALID",
                            "Unit-local character offsets differ from consolidated text",
                        )
                    if source[unit_byte_start:unit_byte_end] != global_text.encode("utf-8"):
                        raise IntegrityError(
                            "PAGINATION_UNIT_BYTE_OFFSETS_INVALID",
                            "Unit-local UTF-8 offsets differ from consolidated text",
                        )
                    source_chunks.append(global_text)
                if sha256_bytes("".join(source_chunks).encode("utf-8")) != str(
                    row["page_source_sha256"]
                ):
                    raise IntegrityError(
                        "PAGINATION_PAGE_SOURCE_HASH_INVALID",
                        "Page source spans do not reproduce page_source_sha256",
                    )
            html_text = html_bytes.decode("utf-8")
            if html_text.count('class="sheet"') != snapshot.document_page_count:
                raise IntegrityError(
                    "PAGINATION_HTML_PAGE_COUNT_INVALID",
                    "HTML explicit sheet count differs from the manifest",
                )
            if html_text.count('class="visual-slot"') != snapshot.eligible_page_count:
                raise IntegrityError(
                    "PAGINATION_HTML_SLOT_COUNT_INVALID",
                    "HTML visual slot count differs from eligible_page_count",
                )
            validate_pdf(pdf_bytes, snapshot.document_page_count)
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page_model, pdf_page in zip(manifest["pages"], reader.pages, strict=True):
                page_key = page_model["page_key"]
                if page_key is not None and (pdf_page.extract_text() or "").count(page_key) != 1:
                    raise IntegrityError(
                        "PAGINATION_PDF_PAGE_KEY_INVALID",
                        "Eligible page_key must appear exactly once on its corresponding PDF page",
                    )

    @staticmethod
    def _validate_rendered(rendered: RenderedPagination, ledger: SourceLedger) -> None:
        if not rendered.pages:
            raise IntegrityError("PAGINATION_EMPTY_DOCUMENT", "Renderer returned no pages")
        fragments_by_unit: dict[str, list[str]] = {unit.unit_id: [] for unit in ledger.units}
        previous_chapter: str | None = None
        seen_chapters: set[str] = set()
        for page in rendered.pages:
            if page.eligible:
                assert page.chapter_id is not None
                if page.chapter_id != previous_chapter:
                    if page.chapter_id in seen_chapters:
                        raise IntegrityError(
                            "PAGINATION_CHAPTER_NONCONTIGUOUS",
                            "A chapter cannot resume after another chapter starts",
                        )
                    seen_chapters.add(page.chapter_id)
                    previous_chapter = page.chapter_id
            else:
                previous_chapter = None
            for fragment in page.fragments:
                fragments_by_unit[fragment.unit_id].append(fragment.text)
        for unit in ledger.units:
            if "".join(fragments_by_unit[unit.unit_id]) != unit.text:
                raise IntegrityError(
                    "PAGINATION_RENDER_REVERSIBILITY_FAILED",
                    f"Rendered fragments do not reconstruct unit {unit.unit_id!r}",
                )

    @staticmethod
    def _validate_page_ledger(row: Any, manifest_page: dict[str, Any]) -> None:
        expected = {
            "document_page_number": int(row["document_page_number"]),
            "eligible": bool(row["eligible"]),
            "eligible_page_number": row["eligible_page_number"],
            "chapter_id": row["chapter_id"],
            "chapter_page_number": row["chapter_page_number"],
            "page_key": row["page_key"],
            "page_source_sha256": row["page_source_sha256"],
        }
        if any(manifest_page.get(field) != value for field, value in expected.items()):
            raise IntegrityError(
                "PAGINATION_PAGE_LEDGER_MISMATCH",
                "SQLite and manifest page bindings differ",
            )

    @staticmethod
    def _validate_span_ledger(row: Any, manifest_span: dict[str, Any]) -> None:
        fields = (
            "span_order",
            "unit_id",
            "artifact_id",
            "source_sha256",
            "global_char_start",
            "global_char_end",
            "global_byte_start",
            "global_byte_end",
            "unit_char_start",
            "unit_char_end",
            "unit_byte_start",
            "unit_byte_end",
        )
        if any(manifest_span.get(field) != row[field] for field in fields):
            raise IntegrityError(
                "PAGINATION_SPAN_LEDGER_MISMATCH",
                "SQLite and manifest unit spans differ",
            )

    @staticmethod
    def _validate_consolidation_manifest(
        consolidation: TextConsolidation,
        context: WritingContext,
        content: bytes,
    ) -> None:
        try:
            manifest = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "PAGINATION_CONSOLIDATION_MANIFEST_INVALID",
                "Text consolidation manifest is not valid UTF-8 JSON",
            ) from exc
        expected = {
            "context_id": consolidation.context_id,
            "production_set_hash": consolidation.production_set_hash,
            "text_sha256": consolidation.text_sha256,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise IntegrityError(
                "PAGINATION_CONSOLIDATION_MANIFEST_BINDING_INVALID",
                "Text consolidation manifest differs from SQLite provenance",
            )
        contract = manifest.get("contract")
        if contract != {
            "id": context.contract_id,
            "version": context.contract_version,
            "sha256": context.contract_sha256,
        }:
            raise IntegrityError(
                "PAGINATION_CONSOLIDATION_CONTRACT_INVALID",
                "Text consolidation manifest contract differs from WritingContext",
            )

    def _record_retry(self, snapshot: VisualPaginationSnapshot, exc: Exception) -> None:
        with self.database.connection() as connection, self.database.transaction(connection):
            runs = StageRunRepository(connection)
            run = runs.get(snapshot.stage_run_id)
            if run.status is not RunStatus.RUNNING:
                return
            if isinstance(exc, HeliosError):
                code = exc.code
                message = exc.message
            else:
                code = "PAGINATION_UNEXPECTED_ERROR"
                message = str(exc)
            ErrorRepository(connection).add(
                project_id=snapshot.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=True,
            )
            retry = self.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
            runs.update(retry, expected_status=run.status)

    def _checkpoint(self, name: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name)

    @staticmethod
    def _validate_utf8_file(path: Path) -> None:
        path.read_bytes().decode("utf-8")

    @staticmethod
    def _validate_json_file(path: Path) -> None:
        json.loads(path.read_bytes())
