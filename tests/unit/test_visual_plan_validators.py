from typing import Any

from ebook_pipeline.visual_planning.models import ParsedVisualFigure
from ebook_pipeline.visual_planning.validators import bind_figures_to_pagination


def _figure(number: int, page: str, *, section: str = "editorial only") -> ParsedVisualFigure:
    return ParsedVisualFigure(
        number=number,
        name=f"Figure {number}",
        editorial_page=page,
        section=section,
        exact_position="position",
        main_concept="concept",
        conceptual_synthesis="synthesis",
        justification="because",
        objective="explain",
        visual_type="diagram",
        complexity="Editorial direta",
        generation_prompt="prompt",
    )


def _manifest() -> dict[str, Any]:
    return {
        "pages": [
            {
                "eligible": True,
                "eligible_page_number": 1,
                "document_page_number": 3,
                "page_key": "CH01-P001",
                "chapter_id": "CH01",
                "unit_spans": [
                    {"span_order": 1, "unit_id": "CH01_A"},
                    {"span_order": 2, "unit_id": "CH01_B"},
                    {"span_order": 3, "unit_id": "CH01_A"},
                ],
            },
            {
                "eligible": True,
                "eligible_page_number": 2,
                "document_page_number": 4,
                "page_key": "CH01-P002",
                "chapter_id": "CH01",
                "unit_spans": [{"span_order": 1, "unit_id": "CH01_B"}],
            },
        ]
    }


def test_binding_uses_all_distinct_page_spans_and_ignores_section() -> None:
    figures = (
        _figure(1, "CH01-P001", section="CH08 semantic bait"),
        _figure(2, "CH01-P002"),
    )

    bound, findings = bind_figures_to_pagination(figures, _manifest())

    assert findings == ()
    assert bound[0].page_unit_ids == ("CH01_A", "CH01_B")
    assert bound[0].page_unit_span_orders == (1, 2)
    assert bound[0].chapter_id == "CH01"


def test_binding_rejects_missing_duplicate_and_invented_pages() -> None:
    figures = (
        _figure(1, "CH01-P001"),
        _figure(2, "CH01-P001"),
        _figure(3, "CH08-P999"),
    )

    bound, findings = bind_figures_to_pagination(figures, _manifest())

    assert bound == ()
    assert {finding.code for finding in findings} == {
        "VISUAL_PLAN_PAGE_DUPLICATED",
        "VISUAL_PLAN_PAGE_MISSING",
        "VISUAL_PLAN_PAGE_INVENTED",
        "VISUAL_PLAN_FIGURE_COUNT_MISMATCH",
    }


def test_binding_requires_global_number_to_follow_eligible_page_order() -> None:
    figures = (_figure(2, "CH01-P001"), _figure(1, "CH01-P002"))

    bound, findings = bind_figures_to_pagination(figures, _manifest())

    assert bound == ()
    assert [finding.code for finding in findings] == [
        "VISUAL_PLAN_NUMBERING_INVALID",
        "VISUAL_PLAN_NUMBERING_INVALID",
    ]


def test_zero_figures_is_valid_only_for_zero_eligible_pages() -> None:
    empty_bound, empty_findings = bind_figures_to_pagination((), {"pages": []})
    _, nonempty_findings = bind_figures_to_pagination((), _manifest())

    assert empty_bound == ()
    assert empty_findings == ()
    assert {finding.code for finding in nonempty_findings} == {
        "VISUAL_PLAN_PAGE_MISSING",
        "VISUAL_PLAN_FIGURE_COUNT_MISMATCH",
    }
