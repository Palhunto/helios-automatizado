from __future__ import annotations

import json

from ebook_pipeline.academic.models import CurrentWritingInputs
from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import canonical_json_bytes, sha256_bytes
from ebook_pipeline.prompts import ResolvedPrompt
from ebook_pipeline.writing.contracts import ResolvedWritingContract, WritingUnitContract
from ebook_pipeline.writing.models import PreparationDependency, ValidationFinding
from ebook_pipeline.writing.projections import StoredAcademicProjection

_PLANNING_PLACEHOLDER = b"{{PLANEJAMENTO_ACADEMICO_E_RESPOSTAS_CONSOLIDADAS}}"
_UNIT_PLACEHOLDER = b"{{UNIT_ID}}"
_SPEC_PLACEHOLDER = b"{{UNIT_SPEC}}"
_CONTINUITY_PLACEHOLDER = b"{{CONTINUITY}}"
_ACADEMIC_CONTEXT_PLACEHOLDER = b"{{ACADEMIC_CONTEXT}}"


def render_context_package(prompt: ResolvedPrompt, inputs: CurrentWritingInputs) -> bytes:
    if prompt.content.count(_PLANNING_PLACEHOLDER) != 1:
        raise IntegrityError(
            "WRITING_PROMPT_PLACEHOLDER_INVALID",
            "Writing prompt must contain the planning placeholder exactly once",
        )
    planning = (
        inputs.rendered_answers.rstrip(b"\n")
        + "\n\nPLANEJAMENTO ACADÊMICO\n\n".encode()
        + inputs.plan.content
    )
    return prompt.content.replace(_PLANNING_PLACEHOLDER, planning)


def context_manifest(
    *,
    project_id: str,
    inputs: CurrentWritingInputs,
    prompt: ResolvedPrompt,
    contract: ResolvedWritingContract,
    request_prompt: ResolvedPrompt,
    package: bytes,
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "answers": {
                    "artifact_id": inputs.answers.artifact.id,
                    "document_id": inputs.answers.document.id,
                    "sha256": inputs.answers.artifact.sha256,
                },
                "contract": {
                    "id": contract.id,
                    "sha256": contract.sha256,
                    "version": contract.version,
                },
                "package_sha256": sha256_bytes(package),
                "plan": {
                    "artifact_id": inputs.plan.artifact.id,
                    "document_id": inputs.plan.document.id,
                    "sha256": inputs.plan.artifact.sha256,
                },
                "project_id": project_id,
                "request_prompt": {
                    "id": request_prompt.id,
                    "sha256": request_prompt.sha256,
                    "version": request_prompt.version,
                },
                "writing_prompt": {
                    "id": prompt.id,
                    "sha256": prompt.sha256,
                    "version": prompt.version,
                },
            }
        )
        + b"\n"
    )


def render_unit_spec(unit: WritingUnitContract, allow_lists: bool) -> str:
    required = ", ".join(unit.required_headings) if unit.required_headings else "nenhum"
    forbidden = ", ".join(unit.forbidden_headings) if unit.forbidden_headings else "nenhum"
    dependencies = ", ".join(unit.continuity_from) if unit.continuity_from else "nenhuma"
    final = unit.final_heading or "nenhum"
    target_range = unit.target_character_range
    return "\n".join(
        (
            f"unit_id: {unit.unit_id}",
            f"ordem: {unit.order}",
            f"caracteres_com_espacos: {target_range.min}..{target_range.max}",
            f"continuity_from: {dependencies}",
            f"headings_obrigatorios: {required}",
            f"headings_proibidos: {forbidden}",
            f"heading_final: {final}",
            f"listas_permitidas: {'sim' if allow_lists else 'não'}",
        )
    )


def render_continuity(contents: tuple[tuple[PreparationDependency, bytes], ...]) -> str:
    if not contents:
        return "Nenhuma dependência textual declarada pelo contract."
    blocks: list[str] = []
    for dependency, content in contents:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError(
                "WRITING_DEPENDENCY_ENCODING_INVALID",
                f"Accepted dependency {dependency.dependency_unit_id!r} is not UTF-8",
            ) from exc
        blocks.append(
            f'<unidade_anterior id="{dependency.dependency_unit_id}">\n{text}\n</unidade_anterior>'
        )
    return "\n\n".join(blocks)


def render_unit_request(
    prompt: ResolvedPrompt,
    unit: WritingUnitContract,
    *,
    allow_lists: bool,
    continuity: str,
    academic_context: bytes | None = None,
) -> bytes:
    for placeholder in (_UNIT_PLACEHOLDER, _SPEC_PLACEHOLDER, _CONTINUITY_PLACEHOLDER):
        if prompt.content.count(placeholder) != 1:
            raise IntegrityError(
                "WRITING_REQUEST_PLACEHOLDER_INVALID",
                "Unit request prompt placeholders must each occur exactly once",
            )
    rendered = (
        prompt.content.replace(_UNIT_PLACEHOLDER, unit.unit_id.encode("utf-8"))
        .replace(_SPEC_PLACEHOLDER, render_unit_spec(unit, allow_lists).encode("utf-8"))
        .replace(_CONTINUITY_PLACEHOLDER, continuity.encode("utf-8"))
    )
    placeholder_count = rendered.count(_ACADEMIC_CONTEXT_PLACEHOLDER)
    if academic_context is None:
        if placeholder_count:
            raise IntegrityError(
                "WRITING_ACADEMIC_CONTEXT_REQUIRED",
                "Unit request prompt requires a frozen academic projection",
            )
        return rendered
    if placeholder_count != 1:
        raise IntegrityError(
            "WRITING_ACADEMIC_CONTEXT_PLACEHOLDER_INVALID",
            "Academic context placeholder must occur exactly once",
        )
    try:
        academic_context.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(
            "WRITING_ACADEMIC_CONTEXT_ENCODING_INVALID",
            "Frozen academic projection must be valid UTF-8",
        ) from exc
    return rendered.replace(_ACADEMIC_CONTEXT_PLACEHOLDER, academic_context)


def preparation_manifest(
    *,
    project_id: str,
    context_id: str,
    unit: WritingUnitContract,
    contract: ResolvedWritingContract,
    request_prompt: ResolvedPrompt,
    request_sha256: str,
    dependencies: tuple[PreparationDependency, ...],
    academic_projection: StoredAcademicProjection | None = None,
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "context_id": context_id,
                "contract": {
                    "id": contract.id,
                    "sha256": contract.sha256,
                    "version": contract.version,
                },
                "continuity": [
                    {
                        "accepted_version": dependency.accepted_version,
                        "artifact_id": dependency.artifact_id,
                        "sha256": dependency.sha256,
                        "submission_id": dependency.submission_id,
                        "unit_id": dependency.dependency_unit_id,
                    }
                    for dependency in dependencies
                ],
                "academic_projection": (
                    None
                    if academic_projection is None
                    else {
                        "artifact_id": academic_projection.artifact_id,
                        "plan_artifact_id": academic_projection.plan_artifact_id,
                        "plan_document_id": academic_projection.plan_document_id,
                        "plan_sha256": academic_projection.plan_sha256,
                        "projection_version": academic_projection.projection_version,
                        "selector": json.loads(academic_projection.selector_json),
                        "sha256": academic_projection.academic_context_sha256,
                        "unit_id": academic_projection.unit_id,
                    }
                ),
                "project_id": project_id,
                "request_prompt": {
                    "id": request_prompt.id,
                    "sha256": request_prompt.sha256,
                    "version": request_prompt.version,
                },
                "request_sha256": request_sha256,
                "unit": unit.model_dump(mode="json"),
            }
        )
        + b"\n"
    )


def validation_report_bytes(
    *,
    unit_id: str,
    character_count: int,
    disposition: str,
    findings: tuple[ValidationFinding, ...],
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "character_count": character_count,
                "disposition": disposition,
                "findings": [
                    {
                        "code": finding.code,
                        "evidence": finding.evidence,
                        "message": finding.message,
                        "severity": finding.severity.value,
                    }
                    for finding in findings
                ],
                "unit_id": unit_id,
            }
        )
        + b"\n"
    )
