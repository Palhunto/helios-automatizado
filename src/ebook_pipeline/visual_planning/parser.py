from __future__ import annotations

import re
from dataclasses import dataclass

from ebook_pipeline.visual_planning.models import ParsedVisualFigure, ValidationFinding

_HEADER = re.compile(r"(?m)^Imagem ([1-9][0-9]*) — ([^\n]+)$")
_FIELD_LABELS = (
    ("editorial_page", "Página"),
    ("section", "Seção"),
    ("exact_position", "Posição exata no texto"),
    ("main_concept", "Conceito principal a representar"),
    ("conceptual_synthesis", "Síntese conceitual da imagem"),
    ("justification", "Justificativa curta"),
    ("objective", "Objetivo da imagem"),
    ("visual_type", "Tipo de imagem sugerido"),
    ("complexity", "Complexidade visual"),
    ("generation_prompt", "Prompt independente para geração"),
)
_LABEL_PATTERN = re.compile(
    r"(?m)^(" + "|".join(re.escape(label) for _, label in _FIELD_LABELS) + r"):[ \t]*"
)
_COMPLEXITIES = {
    "Editorial direta",
    "Editorial estruturada",
    "Síntese conceitual",
}


@dataclass(frozen=True, slots=True)
class VisualPlanParseResult:
    figures: tuple[ParsedVisualFigure, ...]
    findings: tuple[ValidationFinding, ...]
    observed_figure_count: int


def parse_visual_plan(raw: bytes) -> VisualPlanParseResult:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _failure("VISUAL_PLAN_NOT_UTF8", "O raw não é UTF-8 válido")
    if text.startswith("\ufeff"):
        return _failure("VISUAL_PLAN_BOM_FORBIDDEN", "O raw não pode conter UTF-8 BOM")
    if re.search(r"\r(?!\n)", text):
        return _failure(
            "VISUAL_PLAN_LINE_ENDING_INVALID",
            "O raw só pode usar LF ou CRLF como separador de linha",
        )
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return VisualPlanParseResult((), (), 0)

    headers = list(_HEADER.finditer(normalized))
    if not headers or normalized[: headers[0].start()].strip():
        return _failure(
            "VISUAL_PLAN_HEADER_INVALID",
            "O planejamento deve iniciar com 'Imagem N — nome'",
        )

    figures: list[ParsedVisualFigure] = []
    findings: list[ValidationFinding] = []
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(normalized)
        block = normalized[header.end() : block_end].strip()
        figure_order = index + 1
        name = header.group(2)
        if name != name.strip():
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_FIGURE_NAME_INVALID",
                    "O nome da figura não pode ter whitespace nas bordas",
                    figure_order,
                )
            )
            continue
        matches = list(_LABEL_PATTERN.finditer(block))
        actual_labels = [match.group(1) for match in matches]
        expected_labels = [label for _, label in _FIELD_LABELS]
        if actual_labels != expected_labels:
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_FIELDS_INVALID",
                    "Os campos da figura devem aparecer uma vez, na ordem e com os "
                    "rótulos V2 exatos",
                    figure_order,
                )
            )
            continue
        if block[: matches[0].start()].strip():
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_UNEXPECTED_CONTENT",
                    "Há conteúdo inesperado antes do primeiro campo da figura",
                    figure_order,
                )
            )
            continue
        values: dict[str, str] = {}
        empty = False
        for field_index, ((field_name, _), match) in enumerate(
            zip(_FIELD_LABELS, matches, strict=True)
        ):
            value_end = (
                matches[field_index + 1].start() if field_index + 1 < len(matches) else len(block)
            )
            value = block[match.end() : value_end].strip()
            if not value:
                findings.append(
                    ValidationFinding(
                        "VISUAL_PLAN_FIELD_EMPTY",
                        f"O campo {match.group(1)!r} está vazio",
                        figure_order,
                    )
                )
                empty = True
            values[field_name] = value
        if empty:
            continue
        if values["complexity"] not in _COMPLEXITIES:
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_COMPLEXITY_INVALID",
                    "Complexidade visual deve usar um dos três valores canônicos do V2",
                    figure_order,
                )
            )
            continue
        figures.append(
            ParsedVisualFigure(
                number=int(header.group(1)),
                name=name,
                **values,
            )
        )
    return VisualPlanParseResult(tuple(figures), tuple(findings), len(headers))


def _failure(code: str, message: str) -> VisualPlanParseResult:
    return VisualPlanParseResult((), (ValidationFinding(code, message),), 0)
