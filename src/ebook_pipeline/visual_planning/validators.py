from __future__ import annotations

from typing import Any

from ebook_pipeline.visual_planning.models import (
    BoundVisualFigure,
    ParsedVisualFigure,
    ValidationFinding,
)


def bind_figures_to_pagination(
    figures: tuple[ParsedVisualFigure, ...],
    manifest: dict[str, Any],
) -> tuple[tuple[BoundVisualFigure, ...], tuple[ValidationFinding, ...]]:
    pages = [page for page in manifest["pages"] if page["eligible"]]
    pages.sort(key=lambda page: int(page["eligible_page_number"]))
    expected_keys = [str(page["page_key"]) for page in pages]
    expected_set = set(expected_keys)
    findings: list[ValidationFinding] = []

    actual_keys = [figure.editorial_page for figure in figures]
    counts = {page_key: actual_keys.count(page_key) for page_key in set(actual_keys)}
    for page_key in sorted(key for key, count in counts.items() if count > 1):
        findings.append(
            ValidationFinding(
                "VISUAL_PLAN_PAGE_DUPLICATED",
                f"A page_key {page_key!r} aparece mais de uma vez",
                page_key=page_key,
            )
        )
    for page_key in expected_keys:
        if page_key not in counts:
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_PAGE_MISSING",
                    f"A página elegível {page_key!r} não possui figura",
                    page_key=page_key,
                )
            )
    for page_key in sorted(set(actual_keys) - expected_set):
        findings.append(
            ValidationFinding(
                "VISUAL_PLAN_PAGE_INVENTED",
                f"A page_key {page_key!r} não existe no snapshot",
                page_key=page_key,
            )
        )
    if len(figures) != len(pages):
        findings.append(
            ValidationFinding(
                "VISUAL_PLAN_FIGURE_COUNT_MISMATCH",
                f"Foram recebidas {len(figures)} figuras para {len(pages)} páginas elegíveis",
            )
        )

    by_key = {
        figure.editorial_page: figure for figure in figures if counts[figure.editorial_page] == 1
    }
    bound: list[BoundVisualFigure] = []
    for figure_order, page in enumerate(pages, start=1):
        page_key = str(page["page_key"])
        figure = by_key.get(page_key)
        if figure is None:
            continue
        if figure.number != figure_order:
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_NUMBERING_INVALID",
                    f"A figura de {page_key} deve ser Imagem {figure_order}",
                    figure_order,
                    page_key,
                )
            )
        unit_ids: list[str] = []
        span_orders: list[int] = []
        for span in sorted(page["unit_spans"], key=lambda item: int(item["span_order"])):
            unit_id = str(span["unit_id"])
            if unit_id not in unit_ids:
                unit_ids.append(unit_id)
                span_orders.append(int(span["span_order"]))
        if not unit_ids:
            findings.append(
                ValidationFinding(
                    "VISUAL_PLAN_PAGE_UNITS_MISSING",
                    f"A página elegível {page_key!r} não possui unit_spans",
                    figure_order,
                    page_key,
                )
            )
            continue
        bound.append(
            BoundVisualFigure(
                editorial=figure,
                figure_order=figure_order,
                document_page_number=int(page["document_page_number"]),
                eligible_page_number=int(page["eligible_page_number"]),
                page_key=page_key,
                chapter_id=str(page["chapter_id"]),
                page_unit_ids=tuple(unit_ids),
                page_unit_span_orders=tuple(span_orders),
            )
        )
    if findings:
        return (), tuple(findings)
    return tuple(bound), ()
