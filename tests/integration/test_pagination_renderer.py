from pathlib import Path

import pytest
from pypdf import PdfReader

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.pagination.contracts import PaginationLayoutRegistry
from ebook_pipeline.pagination.models import ConsolidationMemberSource
from ebook_pipeline.pagination.renderer import ChromiumPaginationRenderer
from ebook_pipeline.pagination.source_map import build_source_ledger


def _ledger(repository_root: Path, *, unbreakable: bool = False):  # type: ignore[no-untyped-def]
    layout = PaginationLayoutRegistry(
        repository_root / "pagination_layouts" / "registry.yaml"
    ).resolve("helios_pagination_layout", 1)
    members = []
    for mapping in layout.model.ordered_units():
        if mapping.unit_id == "CH01_A":
            text = (
                "x" * 2000
                if unbreakable
                else ("Conceito acadêmico com ação coordenada e café. " * 800).strip()
            )
        elif mapping.unit_id == "CH01_B":
            text = (
                "Continuidade do capítulo.\n\n"
                "Exercícios analíticos e aplicados\n\n"
                "Questão aplicada final."
            )
        else:
            text = f"Conteúdo editorial observado da unidade {mapping.unit_id}."
        content = text.encode("utf-8")
        members.append(
            ConsolidationMemberSource(
                unit_id=mapping.unit_id,
                unit_order=mapping.order,
                artifact_id=f"artifact-{mapping.unit_id}",
                sha256=sha256_bytes(content),
                content=content,
            )
        )
    consolidated = b"\n\n".join(item.content for item in members)
    ledger = build_source_ledger(
        consolidated_bytes=consolidated,
        separator="\n\n",
        members=tuple(members),
        layout=layout.model,
    )
    return layout, ledger


def test_local_chromium_renders_explicit_a4_pages_slots_and_reversible_spans(
    repository_root: Path, tmp_path: Path
) -> None:
    layout, ledger = _ledger(repository_root)
    renderer = ChromiumPaginationRenderer()
    fingerprint = renderer.fingerprint()
    rendered = renderer.render(
        ledger,
        layout,
        input_hash="1" * 64,
        renderer=fingerprint,
    )

    assert len(rendered.pages) >= 10
    eligible = [page for page in rendered.pages if page.eligible]
    assert len(eligible) >= 8
    assert eligible[0].page_key == "CH01-P001"
    assert [page.eligible_page_number for page in eligible] == list(
        range(1, len(eligible) + 1)
    )
    assert all(page.unit_spans for page in rendered.pages)
    assert all(
        span.global_char_end > span.global_char_start
        for page in rendered.pages
        for span in page.unit_spans
    )
    html = rendered.html.decode("utf-8")
    assert html.count('class="visual-slot"') == len(eligible)
    assert ">Capítulo 1<" not in html
    assert "<h2" in html
    assert ">Exercícios analíticos e aplicados" in html
    assert html.count('class="sheet"') == len(rendered.pages)
    output = tmp_path / "canonical-pagination.pdf"
    output.write_bytes(rendered.pdf)
    reader = PdfReader(output)
    assert len(reader.pages) == len(rendered.pages)
    assert all(abs(float(page.mediabox.width) - 595.2756) < 0.5 for page in reader.pages)


def test_local_chromium_fails_closed_on_unbreakable_overflow(repository_root: Path) -> None:
    layout, ledger = _ledger(repository_root, unbreakable=True)
    renderer = ChromiumPaginationRenderer()
    fingerprint = renderer.fingerprint()

    with pytest.raises(IntegrityError) as captured:
        renderer.render(
            ledger,
            layout,
            input_hash="2" * 64,
            renderer=fingerprint,
        )
    assert captured.value.code == "PAGINATION_UNBREAKABLE_CONTENT"
