from __future__ import annotations

import sqlite3
from builtins import list as List
from dataclasses import asdict

from ebook_pipeline.core.errors import NotFoundError
from ebook_pipeline.visual_planning.models import VisualAnchor, VisualFinalization


class AnchorRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, anchor: VisualAnchor) -> None:
        values = asdict(anchor)
        self.connection.execute(
            f"INSERT INTO visual_anchors ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def get(self, anchor_id: str) -> VisualAnchor:
        row = self.connection.execute(
            "SELECT * FROM visual_anchors WHERE id = ?", (anchor_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("VISUAL_ANCHOR_NOT_FOUND", "Visual anchor was not found")
        return VisualAnchor(**dict(row))

    def list(self, figure_id: str) -> list[VisualAnchor]:
        return [
            VisualAnchor(**dict(row))
            for row in self.connection.execute(
                "SELECT * FROM visual_anchors WHERE figure_id = ? ORDER BY version", (figure_id,)
            )
        ]

    def latest(self, figure_id: str) -> VisualAnchor | None:
        anchors = self.list(figure_id)
        return anchors[-1] if anchors else None

    def by_input(self, figure_id: str, input_hash: str) -> VisualAnchor | None:
        row = self.connection.execute(
            "SELECT * FROM visual_anchors WHERE figure_id = ? AND input_hash = ?",
            (figure_id, input_hash),
        ).fetchone()
        return None if row is None else VisualAnchor(**dict(row))

    def complete(self, anchor: VisualAnchor) -> None:
        self.connection.execute(
            "UPDATE visual_anchors SET disposition = ?, raw_artifact_id = ?, "
            "report_artifact_id = ?, report_sha256 = ?, unit_id = ?, start_offset = ?, "
            "end_offset = ?, validated_at = ? WHERE id = ? AND disposition = 'processing'",
            (
                anchor.disposition,
                anchor.raw_artifact_id,
                anchor.report_artifact_id,
                anchor.report_sha256,
                anchor.unit_id,
                anchor.start_offset,
                anchor.end_offset,
                anchor.validated_at,
                anchor.id,
            ),
        )


class FinalizationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: VisualFinalization, anchors: list[VisualAnchor]) -> None:
        values = asdict(item)
        self.connection.execute(
            f"INSERT INTO visual_finalizations ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        self.connection.executemany(
            "INSERT INTO visual_finalization_anchors VALUES (?, ?, ?, ?, ?)",
            [(item.id, item.project_id, item.visual_plan_id, a.figure_id, a.id) for a in anchors],
        )

    def get(self, finalization_id: str) -> VisualFinalization:
        row = self.connection.execute(
            "SELECT * FROM visual_finalizations WHERE id = ?", (finalization_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("VISUAL_FINALIZATION_NOT_FOUND", "Finalization was not found")
        return VisualFinalization(**dict(row))

    def list(self, project_id: str) -> list[VisualFinalization]:
        return [
            VisualFinalization(**dict(row))
            for row in self.connection.execute(
                "SELECT * FROM visual_finalizations WHERE project_id = ? ORDER BY accepted_version",
                (project_id,),
            )
        ]

    def anchors(self, finalization_id: str) -> List[VisualAnchor]:
        return [
            VisualAnchor(**dict(row))
            for row in self.connection.execute(
                "SELECT a.* FROM visual_finalization_anchors m "
                "JOIN visual_anchors a ON a.id = m.anchor_id "
                "JOIN visual_figures f ON f.id = m.figure_id "
                "WHERE m.finalization_id = ? ORDER BY f.figure_order",
                (finalization_id,),
            )
        ]

    def complete(self, item: VisualFinalization) -> None:
        self.connection.execute(
            "UPDATE visual_finalizations SET manifest_artifact_id = ?, manifest_sha256 = ?, "
            "accepted_at = ? WHERE id = ? AND accepted_at IS NULL",
            (item.manifest_artifact_id, item.manifest_sha256, item.accepted_at, item.id),
        )
