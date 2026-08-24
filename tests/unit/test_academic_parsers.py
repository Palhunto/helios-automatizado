from __future__ import annotations

import pytest

from ebook_pipeline.academic.models import HeaderStatus
from ebook_pipeline.academic.parsers import (
    decode_academic_text,
    parse_consolidated_answers,
    parse_numbered_entries,
)
from ebook_pipeline.core.errors import AcademicValidationError

TITLES = (
    "Disciplina e contexto",
    "Perfil dos estudantes",
    "Aprendizagem e aplicação",
    "Estrutura de capítulos",
    "Diretrizes acadêmicas, metodológicas, bibliográficas e editoriais",
)


def answers_text(anchor: str, *, header: str | None = None) -> str:
    lines = [] if header is None else [header]
    for number, title in enumerate(TITLES, start=1):
        if anchor == "number_title":
            lines.append(f"{number}. {title}")
        elif anchor == "number":
            lines.append(f"{number})")
        else:
            lines.append(title)
        lines.append(f"[CONFIRMADO] Conteúdo {number}.")
    return "\n".join(lines)


@pytest.mark.parametrize("anchor", ["number_title", "number", "title"])
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("RESPOSTAS CONSOLIDADAS PARA O PLANEJAMENTO DO EBOOK", HeaderStatus.PRESENT),
        (
            "  respostas   consolidadas para o planejamento do ebook ",
            HeaderStatus.NORMALIZED_VARIANT,
        ),
        (None, HeaderStatus.MISSING),
    ],
)
def test_answers_parser_accepts_deterministic_anchor_and_header_variants(
    anchor: str, header: str | None, expected: HeaderStatus
) -> None:
    parsed = parse_consolidated_answers(answers_text(anchor, header=header))
    assert [answer.question_number for answer in parsed.answers] == [1, 2, 3, 4, 5]
    assert parsed.header_status is expected


def test_answers_parser_rejects_contradictory_number_and_title() -> None:
    text = answers_text("number_title").replace(
        "2. Perfil dos estudantes", "2. Estrutura de capítulos"
    )
    with pytest.raises(AcademicValidationError) as captured:
        parse_consolidated_answers(text)
    assert captured.value.code == "ANSWERS_ANCHOR_CONTRADICTION"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.replace("3. Aprendizagem e aplicação\n", ""),
        lambda value: value.replace("2. Perfil dos estudantes", "1. Perfil dos estudantes"),
        lambda value: value.replace("[CONFIRMADO] Conteúdo 4.", "[APROVADO] Conteúdo 4."),
        lambda value: value.replace("[CONFIRMADO] Conteúdo 5.", "[CONFIRMADO]"),
    ],
)
def test_answers_parser_rejects_missing_duplicate_unknown_and_empty(
    mutation: object,
) -> None:
    text = mutation(answers_text("number_title"))  # type: ignore[operator]
    with pytest.raises(AcademicValidationError):
        parse_consolidated_answers(text)


def test_academic_text_requires_utf8() -> None:
    with pytest.raises(AcademicValidationError) as captured:
        decode_academic_text(b"\xff")
    assert captured.value.code == "ACADEMIC_ENCODING_INVALID"


def test_questionnaire_parser_preserves_multiline_text_and_trims_trailing_blanks() -> None:
    parsed = parse_numbered_entries("\n1. Primeira linha\ncontinuação\n2. B\n3. C\n4. D\n5. E\n\n")
    assert parsed[0] == (1, "Primeira linha\ncontinuação")
    assert parsed[-1] == (5, "E")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("[CONFIRMADO] órfão\n" + answers_text("number_title"), "ANSWERS_MARKER_WITHOUT_QUESTION"),
        (
            answers_text("number_title").replace("[CONFIRMADO] Conteúdo 2.\n3.", "3."),
            "ANSWERS_BLOCK_MISSING",
        ),
        ("texto solto\n" + answers_text("number_title"), "ANSWERS_UNASSIGNED_TEXT"),
        (
            answers_text("number_title").replace(
                "[CONFIRMADO] Conteúdo 5.", "[CONFIRMADO] Conteúdo 5.\ncontinuação"
            ),
            "",
        ),
    ],
)
def test_answers_parser_handles_block_boundaries_and_unassigned_text(text: str, code: str) -> None:
    if not code:
        parsed = parse_consolidated_answers(text)
        assert parsed.answers[-1].blocks[0].text.endswith("continuação")
        return
    with pytest.raises(AcademicValidationError) as captured:
        parse_consolidated_answers(text)
    assert captured.value.code == code
