from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import cast

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.visual_planning.models import (
    VisualFigure,
    VisualPlan,
    VisualPlanDisposition,
)


def _plan(row: sqlite3.Row) -> VisualPlan:
    return VisualPlan(**dict(row))


class VisualPlanRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, plan: VisualPlan) -> None:
        fields = tuple(plan.__dataclass_fields__)
        try:
            self.connection.execute(
                f"INSERT INTO visual_plans({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)})",
                tuple(getattr(plan, field) for field in fields),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "VISUAL_PLAN_CONFLICT", f"Could not create visual plan: {exc}"
            ) from exc

    def get(self, plan_id: str) -> VisualPlan:
        row = self.connection.execute(
            "SELECT * FROM visual_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("VISUAL_PLAN_NOT_FOUND", f"Visual plan {plan_id!r} was not found")
        return _plan(row)

    def by_input_hash(self, project_id: str, input_hash: str) -> VisualPlan | None:
        row = self.connection.execute(
            "SELECT * FROM visual_plans WHERE project_id = ? AND input_hash = ?",
            (project_id, input_hash),
        ).fetchone()
        return None if row is None else _plan(row)

    def latest(self, project_id: str) -> VisualPlan | None:
        row = self.connection.execute(
            "SELECT * FROM visual_plans WHERE project_id = ? ORDER BY raw_version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else _plan(row)

    def by_version(self, project_id: str, version: int) -> VisualPlan | None:
        row = self.connection.execute(
            "SELECT * FROM visual_plans WHERE project_id = ? AND raw_version = ?",
            (project_id, version),
        ).fetchone()
        return None if row is None else _plan(row)

    def list(self, project_id: str) -> list[VisualPlan]:
        rows = self.connection.execute(
            "SELECT * FROM visual_plans WHERE project_id = ? ORDER BY raw_version", (project_id,)
        ).fetchall()
        return [_plan(row) for row in rows]

    def complete(
        self,
        plan: VisualPlan,
        *,
        disposition: str,
        raw_artifact_id: str,
        report_artifact_id: str,
        report_sha256: str,
        figure_count: int,
        validated_at: str,
    ) -> VisualPlan:
        cursor = self.connection.execute(
            "UPDATE visual_plans SET disposition = ?, raw_artifact_id = ?, "
            "validation_report_artifact_id = ?, validation_report_sha256 = ?, "
            "figure_count = ?, validated_at = ? WHERE id = ? AND disposition = 'processing'",
            (
                disposition,
                raw_artifact_id,
                report_artifact_id,
                report_sha256,
                figure_count,
                validated_at,
                plan.id,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(plan.id)
            expected = (
                disposition,
                raw_artifact_id,
                report_artifact_id,
                report_sha256,
                figure_count,
                validated_at,
            )
            actual = (
                current.disposition,
                current.raw_artifact_id,
                current.validation_report_artifact_id,
                current.validation_report_sha256,
                current.figure_count,
                current.validated_at,
            )
            if actual == expected:
                return current
            raise ConflictError(
                "VISUAL_PLAN_CONCURRENT_UPDATE",
                "Visual plan completion evidence changed concurrently",
            )
        return replace(
            plan,
            disposition=cast(VisualPlanDisposition, disposition),
            raw_artifact_id=raw_artifact_id,
            validation_report_artifact_id=report_artifact_id,
            validation_report_sha256=report_sha256,
            figure_count=figure_count,
            validated_at=validated_at,
        )


class VisualFigureRepository:
    _FIELDS = (
        "id",
        "project_id",
        "visual_plan_id",
        "pagination_snapshot_id",
        "figure_order",
        "number",
        "name",
        "editorial_page",
        "section",
        "exact_position",
        "main_concept",
        "conceptual_synthesis",
        "justification",
        "objective",
        "visual_type",
        "complexity",
        "generation_prompt",
        "document_page_number",
        "eligible_page_number",
        "page_key",
        "chapter_id",
        "editorial_sha256",
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, figure: VisualFigure, span_orders: tuple[int, ...]) -> None:
        try:
            self.connection.execute(
                f"INSERT INTO visual_figures({', '.join(self._FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in self._FIELDS)})",
                tuple(getattr(figure, field) for field in self._FIELDS),
            )
            for span_order, unit_id in zip(span_orders, figure.page_unit_ids, strict=True):
                self.connection.execute(
                    "INSERT INTO visual_figure_page_units(figure_id, project_id, "
                    "visual_plan_id, pagination_snapshot_id, document_page_number, page_key, "
                    "span_order, unit_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        figure.id,
                        figure.project_id,
                        figure.visual_plan_id,
                        figure.pagination_snapshot_id,
                        figure.document_page_number,
                        figure.page_key,
                        span_order,
                        unit_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "VISUAL_FIGURE_CONFLICT", f"Could not create visual figure: {exc}"
            ) from exc

    def list(self, plan_id: str) -> list[VisualFigure]:
        rows = self.connection.execute(
            "SELECT * FROM visual_figures WHERE visual_plan_id = ? ORDER BY figure_order",
            (plan_id,),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, figure_id: str) -> VisualFigure:
        row = self.connection.execute(
            "SELECT * FROM visual_figures WHERE id = ?", (figure_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "VISUAL_FIGURE_NOT_FOUND", f"Visual figure {figure_id!r} was not found"
            )
        return self._from_row(row)

    def _from_row(self, row: sqlite3.Row) -> VisualFigure:
        values = dict(row)
        unit_rows = self.connection.execute(
            "SELECT unit_id FROM visual_figure_page_units WHERE figure_id = ? ORDER BY span_order",
            (row["id"],),
        ).fetchall()
        values["page_unit_ids"] = tuple(str(item["unit_id"]) for item in unit_rows)
        return VisualFigure(**values)
