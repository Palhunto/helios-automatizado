import pytest

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.pagination.validators import validate_anchor_candidate


def _manifest() -> dict[str, object]:
    return {
        "pages": [
            {
                "page_key": "CH01-P001",
                "unit_spans": [
                    {
                        "unit_id": "CH01_A",
                        "global_char_start": 0,
                        "global_char_end": 20,
                    }
                ],
            },
            {
                "page_key": "CH01-P002",
                "unit_spans": [
                    {
                        "unit_id": "CH01_A",
                        "global_char_start": 20,
                        "global_char_end": 39,
                    }
                ],
            },
        ]
    }


def test_anchor_may_repeat_globally_when_unique_in_assigned_page_and_unit_span() -> None:
    text = "conceito único aqui. conceito único lá."

    start, end = validate_anchor_candidate(
        _manifest(),
        text,
        page_key="CH01-P001",
        unit_id="CH01_A",
        anchor_text="conceito",
    )

    assert text[start:end] == "conceito"
    assert text.count("conceito") == 2


def test_anchor_rejects_duplicate_inside_assigned_page_unit_span() -> None:
    manifest = _manifest()
    pages = manifest["pages"]
    assert isinstance(pages, list)
    first = pages[0]
    assert isinstance(first, dict)
    spans = first["unit_spans"]
    assert isinstance(spans, list)
    span = spans[0]
    assert isinstance(span, dict)
    span["global_char_end"] = 39

    with pytest.raises(IntegrityError) as captured:
        validate_anchor_candidate(
            manifest,
            "conceito único aqui. conceito único lá.",
            page_key="CH01-P001",
            unit_id="CH01_A",
            anchor_text="conceito",
        )
    assert captured.value.code == "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE_UNIT_SPAN"


def test_anchor_rejects_wrong_typed_unit_even_when_literal_exists() -> None:
    with pytest.raises(IntegrityError) as captured:
        validate_anchor_candidate(
            _manifest(),
            "conceito único aqui. conceito único lá.",
            page_key="CH01-P001",
            unit_id="CH01_B",
            anchor_text="conceito",
        )
    assert captured.value.code == "VISUAL_ANCHOR_UNIT_INVALID"
