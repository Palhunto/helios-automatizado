from __future__ import annotations

import re
import unicodedata

from ebook_pipeline.academic.models import (
    ConsolidatedAnswer,
    ConsolidatedAnswers,
    EditorialBlock,
    EditorialMarker,
    HeaderStatus,
)
from ebook_pipeline.core.errors import AcademicValidationError

ANSWER_HEADER = "RESPOSTAS CONSOLIDADAS PARA O PLANEJAMENTO DO EBOOK"
ANSWER_TITLES = (
    "Disciplina e contexto",
    "Perfil dos estudantes",
    "Aprendizagem e aplicação",
    "Estrutura de capítulos",
    "Diretrizes acadêmicas, metodológicas, bibliográficas e editoriais",
)
MARKERS = {
    "CONFIRMADO": EditorialMarker.CONFIRMADO,
    "COMPLEMENTO PROPOSTO": EditorialMarker.COMPLEMENTO_PROPOSTO,
    "PREMISSA EDITORIAL": EditorialMarker.PREMISSA_EDITORIAL,
    "PENDENTE": EditorialMarker.PENDENTE,
    "CONFLITO": EditorialMarker.CONFLITO,
}
_NUMBERED = re.compile(r"^\s*(\d+)\s*(?:[.)]|[-–—])(?:\s*(.*))?$")
_MARKER = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")


def decode_academic_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcademicValidationError(
            "ACADEMIC_ENCODING_INVALID", "Academic input must be valid UTF-8"
        ) from exc


def normalize_comparison(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    translated = normalized
    for source, target in (
        ("\u00a0", " "),
        ("‘", "'"),
        ("’", "'"),
        ("“", '"'),
        ("”", '"'),
        ("–", "-"),
        ("—", "-"),
    ):
        translated = translated.replace(source, target)
    return " ".join(translated.split()).casefold()


def parse_numbered_entries(text: str) -> tuple[tuple[int, str], ...]:
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_text.split("\n")
    if not any(_NUMBERED.fullmatch(line) for line in lines):
        return _parse_positional_entries(normalized_text)

    entries: list[tuple[int, list[str]]] = []
    for line in lines:
        match = _NUMBERED.fullmatch(line)
        if match is not None:
            number = int(match.group(1))
            entries.append((number, [match.group(2) or ""]))
            continue
        if not entries:
            if line.strip():
                raise AcademicValidationError(
                    "QUESTIONNAIRE_EXTRA_TEXT",
                    "Questionnaire contains text before its first numbered question",
                )
            continue
        entries[-1][1].append(line)

    while entries and not any(part.strip() for part in entries[-1][1]):
        entries[-1][1].pop()
        if entries[-1][1]:
            break
        break
    numbers = [number for number, _ in entries]
    if numbers != [1, 2, 3, 4, 5]:
        raise AcademicValidationError(
            "QUESTIONNAIRE_SEQUENCE_INVALID",
            "Questionnaire must contain exactly questions 1, 2, 3, 4, 5 in order",
            evidence={"observed_numbers": numbers},
        )
    result: list[tuple[int, str]] = []
    for number, parts in entries:
        observed = "\n".join(parts).strip()
        if not observed:
            raise AcademicValidationError(
                "QUESTIONNAIRE_QUESTION_EMPTY", f"Question {number} is empty"
            )
        result.append((number, observed))
    return tuple(result)


def _parse_positional_entries(text: str) -> tuple[tuple[int, str], ...]:
    stripped = text.strip()
    if not stripped:
        raise AcademicValidationError(
            "QUESTIONNAIRE_SEQUENCE_INVALID",
            "Questionnaire must contain exactly five non-empty questions",
            evidence={"observed_count": 0, "numbering": "absent"},
        )

    blank_separated = re.split(r"\n\s*\n+", stripped)
    if len(blank_separated) == 5:
        blocks = tuple(block.strip() for block in blank_separated)
    elif len(blank_separated) == 1:
        blocks = tuple(line.strip() for line in stripped.split("\n") if line.strip())
    else:
        blocks = ()

    if len(blocks) != 5 or any(not block for block in blocks):
        raise AcademicValidationError(
            "QUESTIONNAIRE_UNNUMBERED_AMBIGUOUS",
            "An unnumbered questionnaire must contain exactly five unambiguous blocks",
            evidence={"observed_count": len(blocks), "numbering": "absent"},
        )
    return tuple(enumerate(blocks, start=1))


def _title_number(value: str) -> int | None:
    normalized = normalize_comparison(value)
    for index, title in enumerate(ANSWER_TITLES, start=1):
        if normalized == normalize_comparison(title):
            return index
    return None


def _answer_anchor(line: str) -> int | None:
    numbered = _NUMBERED.fullmatch(line)
    if numbered is not None:
        number = int(numbered.group(1))
        remainder = (numbered.group(2) or "").strip()
        title_number = _title_number(remainder) if remainder else None
        if title_number is not None and title_number != number:
            raise AcademicValidationError(
                "ANSWERS_ANCHOR_CONTRADICTION",
                f"Question number {number} contradicts canonical title {title_number}",
            )
        if not remainder or title_number is not None:
            return number
    return _title_number(line.strip())


def parse_consolidated_answers(text: str) -> ConsolidatedAnswers:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first_nonblank = next((index for index, line in enumerate(lines) if line.strip()), None)
    header_status = HeaderStatus.MISSING
    if first_nonblank is not None:
        first = lines[first_nonblank].strip()
        if first == ANSWER_HEADER:
            header_status = HeaderStatus.PRESENT
            del lines[first_nonblank]
        elif normalize_comparison(first) == normalize_comparison(ANSWER_HEADER):
            header_status = HeaderStatus.NORMALIZED_VARIANT
            del lines[first_nonblank]

    answers: list[tuple[int, list[EditorialBlock]]] = []
    current_number: int | None = None
    current_marker: EditorialMarker | None = None
    current_lines: list[str] = []
    source_order = 0

    def finish_block() -> None:
        nonlocal current_marker, current_lines, source_order
        if current_marker is None:
            return
        content = "\n".join(current_lines).strip()
        if not content:
            raise AcademicValidationError(
                "ANSWERS_BLOCK_EMPTY", "Every editorial marker must contain text"
            )
        source_order += 1
        assert answers
        answers[-1][1].append(EditorialBlock(current_marker, content, source_order))
        current_marker = None
        current_lines = []

    for line in lines:
        anchor = _answer_anchor(line)
        if anchor is not None:
            finish_block()
            if current_number is not None and not answers[-1][1]:
                raise AcademicValidationError(
                    "ANSWERS_BLOCK_MISSING",
                    f"Question {current_number} has no editorial block",
                )
            current_number = anchor
            answers.append((anchor, []))
            continue

        marker_match = _MARKER.fullmatch(line)
        if marker_match is not None:
            if current_number is None:
                raise AcademicValidationError(
                    "ANSWERS_MARKER_WITHOUT_QUESTION",
                    "Editorial marker appears before a question anchor",
                )
            finish_block()
            marker_name = " ".join(marker_match.group(1).split()).upper()
            marker = MARKERS.get(marker_name)
            if marker is None:
                raise AcademicValidationError(
                    "ANSWERS_MARKER_UNKNOWN", f"Unknown editorial marker [{marker_name}]"
                )
            current_marker = marker
            current_lines = [marker_match.group(2)]
            continue

        if current_marker is not None:
            current_lines.append(line)
        elif line.strip():
            raise AcademicValidationError(
                "ANSWERS_UNASSIGNED_TEXT",
                f"Text is not assigned to a recognized question or marker: {line.strip()!r}",
            )

    finish_block()
    if current_number is not None and answers and not answers[-1][1]:
        raise AcademicValidationError(
            "ANSWERS_BLOCK_MISSING", f"Question {current_number} has no editorial block"
        )
    numbers = [number for number, _ in answers]
    if numbers != [1, 2, 3, 4, 5]:
        raise AcademicValidationError(
            "ANSWERS_SEQUENCE_INVALID",
            "Consolidated answers must contain questions 1, 2, 3, 4, 5 in order",
            evidence={"observed_numbers": numbers},
        )
    return ConsolidatedAnswers(
        header_status=header_status,
        answers=tuple(ConsolidatedAnswer(number, tuple(blocks)) for number, blocks in answers),
    )
