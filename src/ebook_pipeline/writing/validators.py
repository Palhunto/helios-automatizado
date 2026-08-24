from __future__ import annotations

import re

from ebook_pipeline.writing.contracts import WritingContract, WritingUnitContract
from ebook_pipeline.writing.models import (
    FindingSeverity,
    SubmissionDisposition,
    TextValidationReport,
    ValidationFinding,
)

_LIST_LINE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+|\([a-zA-Z]\)\s+)")
_WORD = re.compile(r"[\wÀ-ÖØ-öø-ÿ]+", re.UNICODE)


def _normalized_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paragraph_openings(text: str) -> tuple[str, ...]:
    paragraphs = re.split(r"\n\s*\n+", text.strip())
    result: list[str] = []
    for paragraph in paragraphs:
        match = _WORD.search(paragraph)
        if match is not None:
            result.append(match.group(0))
    return tuple(result)


def validate_submission(
    raw: bytes, contract: WritingContract, unit: WritingUnitContract
) -> TextValidationReport:
    findings: list[ValidationFinding] = []
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
        findings.append(
            ValidationFinding(
                "WRITING_ENCODING_INVALID",
                FindingSeverity.ERROR,
                "Writing submission must be valid UTF-8",
            )
        )
    measured = _normalized_newlines(decoded)
    character_count = len(measured)
    if not measured.strip():
        findings.append(
            ValidationFinding(
                "WRITING_CONTENT_EMPTY",
                FindingSeverity.ERROR,
                "Writing submission must contain non-whitespace text",
            )
        )
    accepted_range = unit.accepted_character_range
    if character_count < accepted_range.min or character_count > accepted_range.max:
        findings.append(
            ValidationFinding(
                "WRITING_CHARACTER_COUNT_INVALID",
                FindingSeverity.ERROR,
                f"Character count {character_count} is outside "
                f"{accepted_range.min}..{accepted_range.max}",
                {
                    "actual": character_count,
                    "maximum": accepted_range.max,
                    "minimum": accepted_range.min,
                },
            )
        )

    standalone = {
        " ".join(line.split()).casefold() for line in measured.split("\n") if line.strip()
    }
    for heading in unit.required_headings:
        if " ".join(heading.split()).casefold() not in standalone:
            findings.append(
                ValidationFinding(
                    "WRITING_REQUIRED_HEADING_MISSING",
                    FindingSeverity.ERROR,
                    f"Required heading {heading!r} is missing",
                    {"heading": heading},
                )
            )
    for heading in unit.forbidden_headings:
        if " ".join(heading.split()).casefold() in standalone:
            findings.append(
                ValidationFinding(
                    "WRITING_FORBIDDEN_HEADING_PRESENT",
                    FindingSeverity.ERROR,
                    f"Forbidden heading {heading!r} is present",
                    {"heading": heading},
                )
            )
    if unit.final_heading is not None:
        heading_key = " ".join(unit.final_heading.split()).casefold()
        indices = [
            index
            for index, line in enumerate(measured.split("\n"))
            if " ".join(line.split()).casefold() == heading_key
        ]
        if len(indices) == 1:
            remaining = measured.split("\n")[indices[0] + 1 :]
            if not any(line.strip() for line in remaining):
                findings.append(
                    ValidationFinding(
                        "WRITING_FINAL_HEADING_INVALID",
                        FindingSeverity.ERROR,
                        "Final heading must be followed by non-empty section content",
                    )
                )
        elif len(indices) > 1:
            findings.append(
                ValidationFinding(
                    "WRITING_FINAL_HEADING_INVALID",
                    FindingSeverity.ERROR,
                    "Final heading must occur exactly once",
                    {"occurrences": len(indices)},
                )
            )

    allow_lists = contract.validation.allow_lists if unit.allow_lists is None else unit.allow_lists
    if not allow_lists:
        list_lines = [
            index
            for index, line in enumerate(measured.split("\n"), start=1)
            if _LIST_LINE.match(line)
        ]
        if list_lines:
            findings.append(
                ValidationFinding(
                    "WRITING_LIST_SYNTAX_FORBIDDEN",
                    FindingSeverity.ERROR,
                    "High-confidence list or enumeration syntax is forbidden",
                    {"lines": list_lines},
                )
            )
    folded = measured.casefold()
    for phrase in contract.validation.forbidden_phrases:
        if phrase.casefold() in folded:
            findings.append(
                ValidationFinding(
                    "WRITING_FORBIDDEN_PHRASE_PRESENT",
                    FindingSeverity.ERROR,
                    f"Forbidden operational phrase {phrase!r} is present",
                    {"phrase": phrase},
                )
            )

    openings = _paragraph_openings(measured)
    policy = contract.validation.linguistic_warnings
    for previous, current in zip(openings, openings[1:], strict=False):
        if policy.repeated_first_word and previous.casefold() == current.casefold():
            findings.append(
                ValidationFinding(
                    "WRITING_REPEATED_FIRST_WORD",
                    FindingSeverity.WARNING,
                    f"Consecutive paragraphs begin with {current!r}",
                )
            )
        if (
            policy.repeated_article
            and previous.casefold() in policy.articles
            and current.casefold() in policy.articles
        ):
            findings.append(
                ValidationFinding(
                    "WRITING_REPEATED_ARTICLE_OPENING",
                    FindingSeverity.WARNING,
                    "Consecutive paragraphs begin with articles",
                )
            )
    run_length = policy.initial_run_length
    if policy.repeated_initial and len(openings) >= run_length:
        for start in range(len(openings) - run_length + 1):
            group = openings[start : start + run_length]
            if len({word[0].casefold() for word in group if word}) == 1:
                findings.append(
                    ValidationFinding(
                        "WRITING_REPEATED_INITIAL",
                        FindingSeverity.WARNING,
                        f"{run_length} paragraph openings share the same initial",
                        {"start_paragraph": start + 1},
                    )
                )

    has_errors = any(item.severity is FindingSeverity.ERROR for item in findings)
    has_warnings = any(item.severity is FindingSeverity.WARNING for item in findings)
    disposition = (
        SubmissionDisposition.REJECTED
        if has_errors
        else SubmissionDisposition.REVIEW_REQUIRED
        if has_warnings
        else SubmissionDisposition.ACCEPTED
    )
    return TextValidationReport(unit.unit_id, character_count, disposition, tuple(findings))
