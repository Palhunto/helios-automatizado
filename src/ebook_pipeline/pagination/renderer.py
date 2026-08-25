from __future__ import annotations

import base64
import html
import importlib.metadata
import io
import platform
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import canonical_hash, sha256_bytes, sha256_file
from ebook_pipeline.pagination.contracts import ResolvedPaginationLayout
from ebook_pipeline.pagination.models import (
    PaginationFragment,
    PaginationPage,
    RenderedPagination,
    RendererFingerprint,
    SourceLedger,
)
from ebook_pipeline.pagination.source_map import fragments_for_unit, spans_for_page, split_fragment

_WORD_BOUNDARY = re.compile(r"(?<=\s)(?=\S)")


@dataclass(slots=True)
class _DraftPage:
    eligible: bool
    chapter_id: str | None
    fragments: list[PaginationFragment]


class ChromiumPaginationRenderer:
    def fingerprint(self) -> RendererFingerprint:
        try:
            with sync_playwright() as driver:
                executable = driver.chromium.executable_path
                executable_sha256 = sha256_file(Path(executable))
                browser = driver.chromium.launch(headless=True)
                browser.close()
        except Exception as exc:
            raise IntegrityError(
                "PAGINATION_RENDERER_UNAVAILABLE",
                f"Could not resolve the managed local Chromium renderer: {exc}",
            ) from exc
        values = {
            "chromium_executable_sha256": executable_sha256,
            "machine": platform.machine(),
            "os_name": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "python_version": platform.python_version(),
            "playwright_version": importlib.metadata.version("playwright"),
            "pypdf_version": importlib.metadata.version("pypdf"),
        }
        return RendererFingerprint(fingerprint=canonical_hash(values), **values)

    def render(
        self,
        ledger: SourceLedger,
        layout: ResolvedPaginationLayout,
        *,
        input_hash: str,
        renderer: RendererFingerprint,
    ) -> RenderedPagination:
        try:
            with sync_playwright() as driver:
                executable_hash = sha256_file(Path(driver.chromium.executable_path))
                if executable_hash != renderer.chromium_executable_sha256:
                    raise IntegrityError(
                        "PAGINATION_RENDERER_CHANGED",
                        "Chromium changed after the snapshot identity was computed",
                    )
                browser: Browser | None = None
                try:
                    browser = driver.chromium.launch(headless=True)
                    page = browser.new_page()
                    shell = self._measurement_shell(layout)
                    page.set_content(shell, wait_until="load")
                    self._require_fonts(page)
                    drafts = self._paginate(page, ledger, layout)
                    pages = self._finalize_pages(drafts, ledger)
                    final_html = self._render_html(pages, layout)
                    page.set_content(final_html.decode("utf-8"), wait_until="load")
                    self._require_fonts(page)
                    raw_pdf = page.pdf(
                        display_header_footer=False,
                        prefer_css_page_size=True,
                        print_background=True,
                    )
                    pdf = canonicalize_pdf(raw_pdf, input_hash)
                    validate_pdf(pdf, len(pages))
                    return RenderedPagination(final_html, pdf, pages, renderer)
                finally:
                    with suppress(Exception):
                        if browser is not None:
                            browser.close()
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(
                "PAGINATION_RENDER_FAILED",
                f"Local Chromium pagination failed: {exc}",
                recoverable=True,
            ) from exc

    def _paginate(
        self,
        page: Page,
        ledger: SourceLedger,
        layout: ResolvedPaginationLayout,
    ) -> list[_DraftPage]:
        drafts: list[_DraftPage] = []
        current: _DraftPage | None = None
        for unit in ledger.units:
            if current is None or unit.starts_new_page:
                if current is not None and current.fragments:
                    drafts.append(current)
                current = _DraftPage(unit.eligible, unit.chapter_id, [])
            elif current.eligible != unit.eligible or current.chapter_id != unit.chapter_id:
                raise IntegrityError(
                    "PAGINATION_UNIT_BOUNDARY_INVALID",
                    "A page would cross an incompatible structural boundary",
                )
            pending = list(fragments_for_unit(unit, layout.model))
            while pending:
                fragment = pending.pop(0)
                assert current is not None
                if (
                    fragment.kind == "heading"
                    and pending
                    and current.fragments
                    and not self._heading_can_stay(
                        page, current, fragment, pending[0], layout
                    )
                ):
                    drafts.append(current)
                    current = _DraftPage(unit.eligible, unit.chapter_id, [])
                if self._fits(
                    page, current.fragments + [fragment], current.eligible, layout
                )["fits"]:
                    current.fragments.append(fragment)
                    continue
                if fragment.kind == "paragraph":
                    split = self._largest_fitting_split(page, current, fragment, layout)
                    if split is not None:
                        left, right = split
                        current.fragments.append(left)
                        drafts.append(current)
                        current = _DraftPage(unit.eligible, unit.chapter_id, [])
                        pending.insert(0, right)
                        continue
                if current.fragments:
                    drafts.append(current)
                    current = _DraftPage(unit.eligible, unit.chapter_id, [])
                    pending.insert(0, fragment)
                    continue
                raise IntegrityError(
                    "PAGINATION_UNBREAKABLE_CONTENT",
                    f"Content in {unit.unit_id!r} cannot fit an empty canonical page",
                )
        if current is not None and current.fragments:
            drafts.append(current)
        if not drafts:
            raise IntegrityError("PAGINATION_EMPTY_DOCUMENT", "Consolidation produced no pages")
        return drafts

    def _largest_fitting_split(
        self,
        page: Page,
        current: _DraftPage,
        fragment: PaginationFragment,
        layout: ResolvedPaginationLayout,
    ) -> tuple[PaginationFragment, PaginationFragment] | None:
        boundaries = [match.start() for match in _WORD_BOUNDARY.finditer(fragment.text)]
        if not boundaries:
            return None
        low = 0
        high = len(boundaries) - 1
        best: tuple[PaginationFragment, PaginationFragment] | None = None
        while low <= high:
            middle = (low + high) // 2
            left, right = split_fragment(fragment, boundaries[middle])
            left_measure = self._fits(
                page, current.fragments + [left], current.eligible, layout
            )
            right_measure = self._fits(page, [right], current.eligible, layout)
            if (
                left_measure["fits"]
                and left_measure["last_line_count"] >= layout.model.typography.orphans
                and right_measure["last_line_count"] >= layout.model.typography.widows
            ):
                best = (left, right)
                low = middle + 1
            elif not left_measure["fits"]:
                high = middle - 1
            else:
                high = middle - 1
        return best

    def _heading_can_stay(
        self,
        page: Page,
        current: _DraftPage,
        heading: PaginationFragment,
        following: PaginationFragment,
        layout: ResolvedPaginationLayout,
    ) -> bool:
        if following.kind != "paragraph":
            return bool(
                self._fits(
                    page,
                    current.fragments + [heading, following],
                    current.eligible,
                    layout,
                )["fits"]
            )
        boundaries = [match.start() for match in _WORD_BOUNDARY.finditer(following.text)]
        for boundary in boundaries:
            prefix, _ = split_fragment(following, boundary)
            measure = self._fits(
                page,
                current.fragments + [heading, prefix],
                current.eligible,
                layout,
            )
            if measure["last_line_count"] >= 2:
                return bool(measure["fits"])
        return False

    def _fits(
        self,
        page: Page,
        fragments: list[PaginationFragment],
        eligible: bool,
        layout: ResolvedPaginationLayout,
    ) -> dict[str, Any]:
        region = layout.model.page.eligible_text if eligible else layout.model.page.noneligible_text
        payload = [
            {
                "kind": item.kind,
                "heading_level": item.heading_level,
                "text": item.text,
            }
            for item in fragments
        ]
        return dict(
            page.evaluate(
                """([items, widthMm, heightMm, epsilon]) => {
                    const measure = document.getElementById('measure');
                    const content = document.getElementById('measure-content');
                    measure.style.width = `${widthMm}mm`;
                    measure.style.height = `${heightMm}mm`;
                    content.replaceChildren();
                    for (const item of items) {
                        const tag = item.kind === 'heading' ? `h${item.heading_level}` : 'p';
                        const node = document.createElement(tag);
                        node.textContent = item.text;
                        content.appendChild(node);
                    }
                    const last = content.lastElementChild;
                    let lineCount = 0;
                    if (last && last.firstChild) {
                        const range = document.createRange();
                        range.selectNodeContents(last);
                        const tops = new Set(
                            Array.from(range.getClientRects()).map(rect => rect.top.toFixed(2))
                        );
                        lineCount = tops.size;
                    }
                    return {
                        fits: content.scrollHeight <= measure.clientHeight + epsilon &&
                            content.scrollWidth <= measure.clientWidth + epsilon,
                        last_line_count: lineCount,
                    };
                }""",
                [
                    payload,
                    region.width_mm,
                    region.height_mm,
                    layout.model.renderer.measurement_epsilon_px,
                ],
            )
        )

    def _finalize_pages(
        self, drafts: list[_DraftPage], ledger: SourceLedger
    ) -> tuple[PaginationPage, ...]:
        chapter_counts: dict[str, int] = {}
        eligible_page_number = 0
        pages: list[PaginationPage] = []
        for document_page_number, draft in enumerate(drafts, start=1):
            chapter_page_number: int | None = None
            page_key: str | None = None
            current_eligible_number: int | None = None
            if draft.eligible:
                assert draft.chapter_id is not None
                eligible_page_number += 1
                current_eligible_number = eligible_page_number
                chapter_page_number = chapter_counts.get(draft.chapter_id, 0) + 1
                chapter_counts[draft.chapter_id] = chapter_page_number
                page_key = f"{draft.chapter_id}-P{chapter_page_number:03d}"
            fragments = tuple(draft.fragments)
            source = "".join(item.text for item in fragments).encode("utf-8")
            pages.append(
                PaginationPage(
                    document_page_number=document_page_number,
                    eligible=draft.eligible,
                    eligible_page_number=current_eligible_number,
                    chapter_id=draft.chapter_id,
                    chapter_page_number=chapter_page_number,
                    page_key=page_key,
                    page_source_sha256=sha256_bytes(source),
                    fragments=fragments,
                    unit_spans=spans_for_page(fragments, ledger.units),
                )
            )
        return tuple(pages)

    def _measurement_shell(self, layout: ResolvedPaginationLayout) -> str:
        css = self._css(layout)
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
            f"{css}</style></head><body>"
            '<div id="measure"><div id="measure-content" class="page-content"></div></div>'
            "</body></html>"
        )

    def _render_html(
        self,
        pages: tuple[PaginationPage, ...],
        layout: ResolvedPaginationLayout,
    ) -> bytes:
        rendered_pages: list[str] = []
        for page in pages:
            content = "".join(self._fragment_html(item) for item in page.fragments)
            page_label = page.page_key or f"DOC-P{page.document_page_number:03d}"
            slot = (
                '<div class="visual-slot" aria-label="reserved visual slot">'
                '<div class="visual-media">Espaço visual reservado</div>'
                '<div class="visual-caption">'
                "Imagem e fonte serão inseridas em milestone posterior."
                "</div>"
                "</div>"
                if page.eligible
                else ""
            )
            region_class = "eligible-content" if page.eligible else "noneligible-content"
            rendered_pages.append(
                f'<section class="sheet" data-document-page="{page.document_page_number}" '
                f'data-page-key="{html.escape(page_label)}">'
                f'<header class="page-header">{html.escape(page_label)}</header>'
                f'<main class="page-content {region_class}">{content}</main>{slot}'
                f'<footer class="page-footer">{page.document_page_number}</footer></section>'
            )
        document = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            '<meta name="generator" content="helios_pagination_snapshot@1">'
            f"<style>{self._css(layout)}</style></head><body>{''.join(rendered_pages)}</body></html>"
        )
        return document.encode("utf-8")

    @staticmethod
    def _fragment_html(fragment: PaginationFragment) -> str:
        tag = f"h{fragment.heading_level}" if fragment.kind == "heading" else "p"
        return (
            f'<{tag} data-unit-id="{html.escape(fragment.unit_id)}" '
            f'data-global-char-start="{fragment.global_char_start}" '
            f'data-global-char-end="{fragment.global_char_end}">'
            f"{html.escape(fragment.text)}</{tag}>"
        )

    @staticmethod
    def _require_fonts(page: Page) -> None:
        page.evaluate(
            """async () => {
                await Promise.all([
                    document.fonts.load("400 10.5pt 'Noto Sans'", "ação café"),
                    document.fonts.load("400 10.5pt 'Noto Serif'", "ação café"),
                ]);
                await document.fonts.ready;
            }"""
        )
        result = page.evaluate(
            """() => ({
                sans: document.fonts.check("10.5pt 'Noto Sans'"),
                serif: document.fonts.check("10.5pt 'Noto Serif'"),
                status: document.fonts.status,
            })"""
        )
        if result != {"sans": True, "serif": True, "status": "loaded"}:
            raise IntegrityError(
                "PAGINATION_FONT_LOAD_FAILED",
                "Pinned layout fonts did not load completely; system fallback is forbidden",
                evidence=dict(result),
            )

    @staticmethod
    def _css(layout: ResolvedPaginationLayout) -> str:
        model = layout.model
        fonts = {item.contract.role: item for item in layout.fonts}
        sans = base64.b64encode(fonts["sans"].path.read_bytes()).decode("ascii")
        serif = base64.b64encode(fonts["serif"].path.read_bytes()).decode("ascii")
        page = model.page
        body = model.typography.body
        headings = {level: getattr(model.typography, f"h{level}") for level in range(1, 4)}
        heading_css = "\n".join(
            f".page-content h{level} {{ margin:0 0 {model.typography.paragraph_gap_mm}mm; "
            f"padding:0; white-space:pre-wrap; font-family:'Noto Sans'; "
            f"font-size:{style.font_size_pt}pt; line-height:{style.line_height}; "
            f"font-weight:{style.font_weight}; break-after:avoid; }}"
            for level, style in headings.items()
        )
        return f"""
@font-face {{ font-family:'Noto Sans'; src:url(data:font/ttf;base64,{sans}) format('truetype');
  font-style:normal; font-weight:100 900; font-display:block; }}
@font-face {{ font-family:'Noto Serif'; src:url(data:font/ttf;base64,{serif}) format('truetype');
  font-style:normal; font-weight:100 900; font-display:block; }}
@page {{ size:{page.width_mm}mm {page.height_mm}mm; margin:{page.print_margin_mm}mm; }}
* {{ box-sizing:border-box; }}
html, body {{ margin:0; padding:0; background:#fff; color:#263238; font-synthesis:none; }}
.sheet {{ position:relative; width:{page.width_mm}mm; height:{page.height_mm}mm;
  break-after:page; overflow:hidden; background:#fff; }}
.sheet:last-child {{ break-after:auto; }}
.page-header, .page-footer {{ position:absolute;
  font:400 7pt/1 'Noto Sans'; color:#536269; letter-spacing:.03em; }}
.page-header {{ left:{page.header.x_mm}mm; top:{page.header.y_mm}mm;
  width:{page.header.width_mm}mm; height:{page.header.height_mm}mm; }}
.page-footer {{ left:{page.footer.x_mm}mm; top:{page.footer.y_mm}mm;
  width:{page.footer.width_mm}mm; height:{page.footer.height_mm}mm; text-align:right; }}
.page-content {{ position:absolute; overflow:hidden; font-family:'Noto Serif';
  font-size:{body.font_size_pt}pt; line-height:{body.line_height}; font-weight:{body.font_weight};
  text-align:{model.typography.text_align}; hyphens:{model.typography.hyphens}; }}
.eligible-content {{ left:{page.eligible_text.x_mm}mm; top:{page.eligible_text.y_mm}mm;
  width:{page.eligible_text.width_mm}mm; height:{page.eligible_text.height_mm}mm; }}
.noneligible-content {{ left:{page.noneligible_text.x_mm}mm; top:{page.noneligible_text.y_mm}mm;
  width:{page.noneligible_text.width_mm}mm; height:{page.noneligible_text.height_mm}mm; }}
.page-content p {{ margin:0 0 {model.typography.paragraph_gap_mm}mm; padding:0;
  white-space:pre-wrap; overflow-wrap:normal; word-break:normal;
  orphans:{model.typography.orphans}; widows:{model.typography.widows}; }}
{heading_css}
.visual-slot {{ position:absolute; left:{page.visual_slot.x_mm}mm; top:{page.visual_slot.y_mm}mm;
  width:{page.visual_slot.width_mm}mm; height:{page.visual_slot.height_mm}mm;
  border:.25mm solid #bdc8cc; background:#f7f9f9; color:#607278; font-family:'Noto Sans'; }}
.visual-media {{ height:{page.visual_media_max_height_mm}mm; display:flex; align-items:center;
  justify-content:center; font-size:9pt; letter-spacing:.04em; }}
.visual-caption {{ height:{page.visual_caption_height_mm}mm; border-top:.2mm solid #d8dfe1;
  padding:2mm 3mm 0; font-size:7pt; }}
#measure {{ position:fixed; left:-10000px; top:0; width:{page.eligible_text.width_mm}mm;
  overflow:hidden; }}
#measure-content {{ position:static; width:100%; height:auto; overflow:visible; }}
"""


def canonicalize_pdf(content: bytes, identity_hash: str) -> bytes:
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata(
        {
            "/CreationDate": "D:20000101000000Z",
            "/Creator": "Helios Canonical Pagination",
            "/ModDate": "D:20000101000000Z",
            "/Producer": "Helios PDF Canonicalization v1",
        }
    )
    identity = bytes.fromhex(identity_hash)
    writer._ID = ArrayObject(  # noqa: SLF001
        [ByteStringObject(identity[:16]), ByteStringObject(identity[16:])]
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def validate_pdf(content: bytes, expected_page_count: int) -> None:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise IntegrityError(
            "PAGINATION_PDF_INVALID", f"Could not read generated PDF: {exc}"
        ) from exc
    if len(reader.pages) != expected_page_count:
        raise IntegrityError(
            "PAGINATION_PDF_PAGE_COUNT_MISMATCH",
            "Generated PDF page count differs from the pagination manifest",
        )
    for number, page in enumerate(reader.pages, start=1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if abs(width - 595.2756) > 0.5 or abs(height - 841.8898) > 0.5:
            raise IntegrityError(
                "PAGINATION_PDF_PAGE_SIZE_INVALID",
                f"PDF page {number} is not A4 portrait",
            )
