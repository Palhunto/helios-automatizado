from __future__ import annotations

import re
import unicodedata

from ebook_pipeline.core.ids import new_id
from ebook_pipeline.writing.models import CitationOccurrence

EXTRACTION_RULE_VERSION = 1
_SURNAME = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+"
_AUTHOR = rf"{_SURNAME}(?:\s+(?:e|&)\s+{_SURNAME}|\s+et\s+al\.)?"
_PARENTHETICAL = re.compile(rf"\((?P<author>{_AUTHOR}),\s*(?P<year>\d{{4}}[a-z]?)\)")
_NARRATIVE = re.compile(rf"(?P<author>{_AUTHOR})\s+\((?P<year>\d{{4}}[a-z]?)\)")


def _normalize_author(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def extract_citations(
    *, project_id: str, submission_id: str, unit_id: str, raw: bytes
) -> tuple[CitationOccurrence, ...]:
    text = raw.decode("utf-8")
    candidates: list[tuple[int, int, str, str, str]] = []
    for pattern in (_PARENTHETICAL, _NARRATIVE):
        for match in pattern.finditer(text):
            candidates.append(
                (
                    match.start(),
                    match.end(),
                    match.group(0),
                    match.group("author"),
                    match.group("year"),
                )
            )
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, str, str]] = []
    for candidate in candidates:
        if any(candidate[0] < item[1] and item[0] < candidate[1] for item in accepted):
            continue
        accepted.append(candidate)
    accepted.sort(key=lambda item: item[0])
    return tuple(
        CitationOccurrence(
            id=new_id(),
            project_id=project_id,
            submission_id=submission_id,
            unit_id=unit_id,
            ordinal=ordinal,
            raw_citation_text=raw_text,
            observed_author=author,
            normalized_author=_normalize_author(author),
            year_text=year_text,
            parsed_year=int(year_text) if year_text.isdigit() else None,
            start_offset=start,
            end_offset=end,
            extraction_rule_version=EXTRACTION_RULE_VERSION,
        )
        for ordinal, (start, end, raw_text, author, year_text) in enumerate(accepted, start=1)
    )


def citation_ledger_bytes(occurrences: tuple[CitationOccurrence, ...]) -> bytes:
    from ebook_pipeline.core.hashing import canonical_json_bytes

    return (
        canonical_json_bytes(
            {
                "boundary": "Citation Ledger != Reference Reconciliation",
                "extraction_rule_version": EXTRACTION_RULE_VERSION,
                "occurrences": [
                    {
                        "end_offset": item.end_offset,
                        "normalized_author": item.normalized_author,
                        "observed_author": item.observed_author,
                        "ordinal": item.ordinal,
                        "parsed_year": item.parsed_year,
                        "raw_citation_text": item.raw_citation_text,
                        "start_offset": item.start_offset,
                        "unit_id": item.unit_id,
                        "year_text": item.year_text,
                    }
                    for item in occurrences
                ],
            }
        )
        + b"\n"
    )
