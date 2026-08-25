from pathlib import Path

import pytest

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.pagination.contracts import PaginationLayoutRegistry
from ebook_pipeline.pagination.models import ConsolidationMemberSource
from ebook_pipeline.pagination.source_map import (
    build_source_ledger,
    fragments_for_unit,
    spans_for_page,
    split_fragment,
)


def _layout(repository_root: Path):  # type: ignore[no-untyped-def]
    return PaginationLayoutRegistry(
        repository_root / "pagination_layouts" / "registry.yaml"
    ).resolve("helios_pagination_layout", 1).model


def _members(repository_root: Path) -> tuple[ConsolidationMemberSource, ...]:
    members = []
    for item in _layout(repository_root).ordered_units():
        text = (
            "Introdução com ação e café."
            if item.unit_id == "INTRO"
            else f"Texto de {item.unit_id} com ação e café."
        )
        if item.unit_id == "CH01_B":
            text += "\n\nExercícios analíticos e aplicados\n\nQuestão final."
        content = text.encode("utf-8")
        members.append(
            ConsolidationMemberSource(
                unit_id=item.unit_id,
                unit_order=item.order,
                artifact_id=f"artifact-{item.unit_id}",
                sha256=sha256_bytes(content),
                content=content,
            )
        )
    return tuple(members)


def test_source_ledger_has_reversible_unicode_and_utf8_offsets(repository_root: Path) -> None:
    members = _members(repository_root)
    separator = "\n\n"
    consolidated = separator.encode().join(item.content for item in members)

    ledger = build_source_ledger(
        consolidated_bytes=consolidated,
        separator=separator,
        members=members,
        layout=_layout(repository_root),
    )

    assert ledger.text_bytes == consolidated
    assert ledger.text.encode("utf-8") == consolidated
    chapter = ledger.units[1]
    assert chapter.unit_id == "CH01_A"
    assert ledger.text[chapter.global_char_start : chapter.global_char_end] == chapter.text
    assert (
        ledger.text_bytes[chapter.global_byte_start : chapter.global_byte_end]
        == chapter.text.encode("utf-8")
    )
    assert chapter.global_byte_end - chapter.global_byte_start > (
        chapter.global_char_end - chapter.global_char_start
    )


def test_fragments_preserve_source_and_only_observed_heading_changes_layout(
    repository_root: Path,
) -> None:
    members = _members(repository_root)
    consolidated = b"\n\n".join(item.content for item in members)
    ledger = build_source_ledger(
        consolidated_bytes=consolidated,
        separator="\n\n",
        members=members,
        layout=_layout(repository_root),
    )
    fragments = fragments_for_unit(ledger.units[2], _layout(repository_root))

    assert "".join(item.text for item in fragments) == ledger.units[2].text
    assert [item.kind for item in fragments] == ["paragraph", "heading", "paragraph"]
    assert all("Capítulo 1" not in item.text for item in fragments)


def test_split_fragment_and_page_spans_remain_reversible(repository_root: Path) -> None:
    members = _members(repository_root)
    consolidated = b"\n\n".join(item.content for item in members)
    ledger = build_source_ledger(
        consolidated_bytes=consolidated,
        separator="\n\n",
        members=members,
        layout=_layout(repository_root),
    )
    fragment = fragments_for_unit(ledger.units[1], _layout(repository_root))[0]
    left, right = split_fragment(fragment, fragment.text.index(" ") + 1)

    assert left.text + right.text == fragment.text
    assert left.global_char_end == right.global_char_start
    assert left.global_byte_end == right.global_byte_start
    spans = spans_for_page((left, right), ledger.units)
    assert len(spans) == 1
    assert spans[0].unit_id == "CH01_A"
    assert spans[0].global_char_start == fragment.global_char_start
    assert spans[0].global_char_end == fragment.global_char_end


def test_source_ledger_rejects_character_estimation_or_inexact_members(
    repository_root: Path,
) -> None:
    members = _members(repository_root)
    with pytest.raises(IntegrityError) as captured:
        build_source_ledger(
            consolidated_bytes=b"different",
            separator="\n\n",
            members=members,
            layout=_layout(repository_root),
        )
    assert captured.value.code == "PAGINATION_CONSOLIDATION_RECONSTRUCTION_MISMATCH"
