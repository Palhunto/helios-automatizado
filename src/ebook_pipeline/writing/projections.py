from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ebook_pipeline.academic.parsers import normalize_comparison
from ebook_pipeline.core.errors import IntegrityError, WritingValidationError
from ebook_pipeline.core.hashing import canonical_json_bytes, sha256_bytes
from ebook_pipeline.writing.contracts import AcademicContextSelector, WritingContract

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class AcademicProjection:
    unit_id: str
    selector_json: str
    projection_version: int
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredAcademicProjection:
    context_id: str
    project_id: str
    unit_id: str
    selector_json: str
    plan_document_id: str
    plan_artifact_id: str
    plan_sha256: str
    projection_version: int
    academic_context_sha256: str
    artifact_id: str
    created_at: str


class AcademicProjectionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_manifest(
        self,
        *,
        context_id: str,
        project_id: str,
        manifest_sha256: str,
        manifest_artifact_id: str,
        created_at: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO writing_context_projection_manifests "
            "(context_id, project_id, projection_version, manifest_sha256, "
            "manifest_artifact_id, created_at) VALUES (?, ?, 1, ?, ?, ?)",
            (context_id, project_id, manifest_sha256, manifest_artifact_id, created_at),
        )

    def add(
        self,
        *,
        context_id: str,
        project_id: str,
        projection: AcademicProjection,
        plan_document_id: str,
        plan_artifact_id: str,
        plan_sha256: str,
        artifact_id: str,
        created_at: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO writing_unit_academic_projections "
            "(context_id, project_id, unit_id, selector_json, plan_document_id, "
            "plan_artifact_id, plan_sha256, projection_version, academic_context_sha256, "
            "artifact_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context_id,
                project_id,
                projection.unit_id,
                projection.selector_json,
                plan_document_id,
                plan_artifact_id,
                plan_sha256,
                projection.projection_version,
                projection.sha256,
                artifact_id,
                created_at,
            ),
        )

    def get(self, context_id: str, unit_id: str) -> StoredAcademicProjection | None:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_academic_projections "
            "WHERE context_id = ? AND unit_id = ?",
            (context_id, unit_id),
        ).fetchone()
        return None if row is None else StoredAcademicProjection(**dict(row))


@dataclass(frozen=True, slots=True)
class _Section:
    level: int
    title: str
    normalized_title: str
    start: int
    end: int
    ancestors: tuple[str, ...]


def _sections(plan: str) -> tuple[_Section, ...]:
    matches = tuple(_HEADING.finditer(plan))
    result: list[_Section] = []
    stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        end = len(plan)
        for following in matches[index + 1 :]:
            if len(following.group(1)) <= level:
                end = following.start()
                break
        title = match.group(2).strip()
        result.append(
            _Section(
                level=level,
                title=title,
                normalized_title=normalize_comparison(title),
                start=match.start(),
                end=end,
                ancestors=tuple(item[1] for item in stack),
            )
        )
        stack.append((level, normalize_comparison(title)))
    return tuple(result)


def _matches(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value, flags=re.IGNORECASE) is not None
    except re.error as exc:
        raise IntegrityError(
            "WRITING_ACADEMIC_SELECTOR_INVALID", f"Invalid academic selector regex: {exc}"
        ) from exc


def _select(
    plan: str, parsed: tuple[_Section, ...], selector: AcademicContextSelector, unit_id: str
) -> bytes:
    selected: list[_Section] = []
    for rule in selector.sections:
        candidates = [
            section
            for section in parsed
            if _matches(rule.heading_pattern, section.normalized_title)
            and (rule.heading_level is None or section.level == rule.heading_level)
            and (
                rule.ancestor_pattern is None
                or any(_matches(rule.ancestor_pattern, ancestor) for ancestor in section.ancestors)
            )
        ]
        if len(candidates) > 1:
            raise WritingValidationError(
                "WRITING_ACADEMIC_PROJECTION_AMBIGUOUS",
                f"Academic selector for {unit_id!r} matched multiple sections",
                evidence={
                    "heading_level": rule.heading_level,
                    "heading_pattern": rule.heading_pattern,
                    "matches": len(candidates),
                },
            )
        if not candidates:
            if rule.required:
                raise WritingValidationError(
                    "WRITING_ACADEMIC_PROJECTION_MISSING",
                    f"Required Academic Plan section for {unit_id!r} was not found",
                    evidence={
                        "heading_level": rule.heading_level,
                        "heading_pattern": rule.heading_pattern,
                    },
                )
            continue
        selected.append(candidates[0])
    if not selected:
        raise WritingValidationError(
            "WRITING_ACADEMIC_PROJECTION_EMPTY",
            f"Academic projection for {unit_id!r} contains no proven section",
        )
    unique = {section.start: section for section in selected}
    ordered = sorted(unique.values(), key=lambda item: item.start)
    content = "\n\n".join(plan[item.start : item.end].strip() for item in ordered)
    return (content.rstrip() + "\n").encode("utf-8")


def resolve_academic_projections(
    plan_bytes: bytes, contract: WritingContract
) -> tuple[AcademicProjection, ...]:
    try:
        plan = plan_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WritingValidationError(
            "WRITING_ACADEMIC_PLAN_ENCODING_INVALID", "Academic Plan must be valid UTF-8"
        ) from exc
    parsed = _sections(plan)
    if not parsed:
        raise WritingValidationError(
            "WRITING_ACADEMIC_PLAN_STRUCTURE_INVALID",
            "Academic Plan has no Markdown headings for deterministic projection",
        )
    projections: list[AcademicProjection] = []
    for unit in contract.ordered_units():
        if unit.academic_context is None:
            raise WritingValidationError(
                "WRITING_ACADEMIC_SELECTOR_MISSING",
                f"Contract unit {unit.unit_id!r} has no academic context selector",
            )
        selector_bytes = canonical_json_bytes(unit.academic_context.model_dump(mode="json"))
        content = _select(plan, parsed, unit.academic_context, unit.unit_id)
        projections.append(
            AcademicProjection(
                unit_id=unit.unit_id,
                selector_json=selector_bytes.decode("utf-8"),
                projection_version=unit.academic_context.projection_version,
                content=content,
                sha256=sha256_bytes(content),
            )
        )
    return tuple(projections)


def projection_manifest_bytes(
    *,
    plan_document_id: str,
    plan_artifact_id: str,
    plan_sha256: str,
    projections: tuple[AcademicProjection, ...],
) -> bytes:
    return canonical_json_bytes(
        {
            "projections": [
                {
                    "academic_context_sha256": item.sha256,
                    "plan_artifact_id": plan_artifact_id,
                    "plan_document_id": plan_document_id,
                    "plan_sha256": plan_sha256,
                    "projection_version": item.projection_version,
                    "selector": json.loads(item.selector_json),
                    "unit_id": item.unit_id,
                }
                for item in projections
            ]
        }
    ) + b"\n"
