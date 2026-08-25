from __future__ import annotations

import sqlite3
from dataclasses import replace

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.pagination.models import PageUnitSpan, PaginationPage, VisualPaginationSnapshot


def _snapshot(row: sqlite3.Row) -> VisualPaginationSnapshot:
    return VisualPaginationSnapshot(**dict(row))


class PaginationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: VisualPaginationSnapshot) -> None:
        try:
            self.connection.execute(
                "INSERT INTO visual_pagination_snapshots("
                "id, project_id, stage_run_id, version, input_hash, consolidation_id, "
                "context_id, production_set_hash, text_artifact_id, text_sha256, "
                "consolidation_manifest_artifact_id, consolidation_manifest_sha256, "
                "writing_contract_id, writing_contract_version, writing_contract_sha256, "
                "layout_id, layout_version, layout_sha256, renderer_fingerprint, html_sha256, "
                "pdf_sha256, manifest_sha256, html_artifact_id, pdf_artifact_id, "
                "manifest_artifact_id, document_page_count, eligible_page_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?)",
                tuple(getattr(item, field) for field in item.__dataclass_fields__),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "PAGINATION_SNAPSHOT_CONFLICT",
                f"Could not create pagination snapshot: {exc}",
            ) from exc

    def get(self, snapshot_id: str) -> VisualPaginationSnapshot:
        row = self.connection.execute(
            "SELECT * FROM visual_pagination_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "PAGINATION_SNAPSHOT_NOT_FOUND",
                f"Pagination snapshot {snapshot_id!r} was not found",
            )
        return _snapshot(row)

    def by_input_hash(self, project_id: str, input_hash: str) -> VisualPaginationSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM visual_pagination_snapshots "
            "WHERE project_id = ? AND input_hash = ?",
            (project_id, input_hash),
        ).fetchone()
        return None if row is None else _snapshot(row)

    def latest(self, project_id: str) -> VisualPaginationSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM visual_pagination_snapshots WHERE project_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else _snapshot(row)

    def set_complete(
        self,
        item: VisualPaginationSnapshot,
        *,
        html_artifact_id: str,
        html_sha256: str,
        pdf_artifact_id: str,
        pdf_sha256: str,
        manifest_artifact_id: str,
        manifest_sha256: str,
        document_page_count: int,
        eligible_page_count: int,
    ) -> VisualPaginationSnapshot:
        cursor = self.connection.execute(
            "UPDATE visual_pagination_snapshots SET html_artifact_id = ?, html_sha256 = ?, "
            "pdf_artifact_id = ?, pdf_sha256 = ?, manifest_artifact_id = ?, "
            "manifest_sha256 = ?, document_page_count = ?, eligible_page_count = ? "
            "WHERE id = ? AND html_artifact_id IS NULL AND pdf_artifact_id IS NULL "
            "AND manifest_artifact_id IS NULL",
            (
                html_artifact_id,
                html_sha256,
                pdf_artifact_id,
                pdf_sha256,
                manifest_artifact_id,
                manifest_sha256,
                document_page_count,
                eligible_page_count,
                item.id,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            expected = (
                html_artifact_id,
                html_sha256,
                pdf_artifact_id,
                pdf_sha256,
                manifest_artifact_id,
                manifest_sha256,
                document_page_count,
                eligible_page_count,
            )
            actual = (
                current.html_artifact_id,
                current.html_sha256,
                current.pdf_artifact_id,
                current.pdf_sha256,
                current.manifest_artifact_id,
                current.manifest_sha256,
                current.document_page_count,
                current.eligible_page_count,
            )
            if actual == expected:
                return current
            raise ConflictError(
                "PAGINATION_SNAPSHOT_CONCURRENT_UPDATE",
                "Pagination snapshot completion evidence changed concurrently",
            )
        return replace(
            item,
            html_artifact_id=html_artifact_id,
            html_sha256=html_sha256,
            pdf_artifact_id=pdf_artifact_id,
            pdf_sha256=pdf_sha256,
            manifest_artifact_id=manifest_artifact_id,
            manifest_sha256=manifest_sha256,
            document_page_count=document_page_count,
            eligible_page_count=eligible_page_count,
        )

    def add_page(self, snapshot: VisualPaginationSnapshot, page: PaginationPage) -> None:
        try:
            self.connection.execute(
                "INSERT INTO visual_pagination_pages("
                "pagination_snapshot_id, project_id, document_page_number, eligible, "
                "eligible_page_number, chapter_id, chapter_page_number, page_key, "
                "page_source_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.project_id,
                    page.document_page_number,
                    int(page.eligible),
                    page.eligible_page_number,
                    page.chapter_id,
                    page.chapter_page_number,
                    page.page_key,
                    page.page_source_sha256,
                ),
            )
            for span in page.unit_spans:
                self._add_span(snapshot, page.document_page_number, span)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "PAGINATION_PAGE_CONFLICT",
                f"Could not persist pagination page: {exc}",
            ) from exc

    def _add_span(
        self,
        snapshot: VisualPaginationSnapshot,
        document_page_number: int,
        span: PageUnitSpan,
    ) -> None:
        self.connection.execute(
            "INSERT INTO visual_pagination_page_unit_spans("
            "pagination_snapshot_id, project_id, document_page_number, span_order, unit_id, "
            "artifact_id, source_sha256, global_char_start, global_char_end, "
            "global_byte_start, global_byte_end, unit_char_start, unit_char_end, "
            "unit_byte_start, unit_byte_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.project_id,
                document_page_number,
                span.span_order,
                span.unit_id,
                span.artifact_id,
                span.source_sha256,
                span.global_char_start,
                span.global_char_end,
                span.global_byte_start,
                span.global_byte_end,
                span.unit_char_start,
                span.unit_char_end,
                span.unit_byte_start,
                span.unit_byte_end,
            ),
        )

    def page_rows(self, snapshot_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM visual_pagination_pages WHERE pagination_snapshot_id = ? "
            "ORDER BY document_page_number",
            (snapshot_id,),
        ).fetchall()

    def span_rows(self, snapshot_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM visual_pagination_page_unit_spans WHERE pagination_snapshot_id = ? "
            "ORDER BY document_page_number, span_order",
            (snapshot_id,),
        ).fetchall()
