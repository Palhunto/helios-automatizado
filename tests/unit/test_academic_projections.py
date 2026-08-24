from __future__ import annotations

from pathlib import Path

import pytest

from ebook_pipeline.core.errors import WritingValidationError
from ebook_pipeline.writing.contracts import WritingContract, WritingContractRegistry
from ebook_pipeline.writing.projections import (
    projection_manifest_bytes,
    resolve_academic_projections,
)


def realistic_academic_plan() -> bytes:
    global_sections = (
        "# PLANEJAMENTO ACADÊMICO DO EBOOK\n\n"
        "# 1. Perfil dos estudantes\nProfissionais em formação continuada.\n\n"
        "# 2. Objetivo geral\nAplicar fundamentos com julgamento crítico.\n\n"
        "# 3. Princípios metodológicos\nFunção didática baseada em casos e análise.\n\n"
        "# 4. Arquitetura geral do ebook\nOito capítulos articulados em progressão.\n\n"
        "## Capítulo 1\nEntrada resumida do capítulo 1 no mapa global.\n\n"
        "## Capítulo 2\nEntrada resumida do capítulo 2 no mapa global.\n\n"
        "# 5. Resultados de aprendizagem\nAnalisar, sintetizar e decidir.\n\n"
        "# 6. Síntese do desenho pedagógico\nProgressão entre conceitos e aplicação.\n\n"
    )
    chapters = "".join(
        (
            f"# CAPÍTULO {number} — TEMA REALISTA {number}\n\n"
            "## Função educacional\nFunção existente no plano.\n\n"
            "## Objetivo de aprendizagem\nObjetivo existente no plano.\n\n"
            f"## Conteúdos centrais\nConteúdo comprovado do capítulo {number}.\n\n"
        )
        for number in range(1, 9)
    )
    return (global_sections + chapters).encode("utf-8")


def _contract() -> WritingContract:
    return WritingContract.model_validate(
        {
            "id": "projection_test",
            "version": 1,
            "separator": "\n",
            "units": [
                {
                    "unit_id": "A",
                    "order": 1,
                    "min_characters": 1,
                    "max_characters": 10,
                    "academic_context": {
                        "projection_version": 9,
                        "sections": [
                            {
                                "heading_pattern": "^capítulo 1$",
                                "ancestor_pattern": "^planejamento dos capítulos$",
                            }
                        ],
                    },
                },
                {
                    "unit_id": "B",
                    "order": 2,
                    "min_characters": 1,
                    "max_characters": 10,
                    "academic_context": {
                        "projection_version": 9,
                        "sections": [{"heading_pattern": "^objetivo geral$"}],
                    },
                },
            ],
        }
    )


def test_all_projections_are_resolved_eagerly_and_manifested() -> None:
    plan = (
        "# Plano\n\n## Objetivo geral\nAprender.\n\n"
        "## Planejamento dos capítulos\n\n### Capítulo 1\n"
        "Função educacional, objetivo, conteúdos e restrições.\n"
    ).encode()
    projections = resolve_academic_projections(plan, _contract())
    assert [item.unit_id for item in projections] == ["A", "B"]
    assert projections[0].projection_version == 9
    assert b"Fun\xc3\xa7\xc3\xa3o educacional" in projections[0].content
    manifest = projection_manifest_bytes(
        plan_document_id="plan-document",
        plan_artifact_id="plan-artifact",
        plan_sha256="a" * 64,
        projections=projections,
    )
    assert b'"academic_context_sha256"' in manifest
    assert b'"projection_version":9' in manifest


def test_missing_late_unit_fails_during_eager_resolution() -> None:
    plan = "# Plano\n\n## Planejamento dos capítulos\n\n### Capítulo 1\nConteúdo.\n"
    with pytest.raises(WritingValidationError) as captured:
        resolve_academic_projections(plan.encode(), _contract())
    assert captured.value.code == "WRITING_ACADEMIC_PROJECTION_MISSING"


def test_ambiguous_selector_is_rejected() -> None:
    plan = (
        "# Plano\n\n## Objetivo geral\nUm.\n\n## Objetivo geral\nDois.\n\n"
        "## Planejamento dos capítulos\n\n### Capítulo 1\nConteúdo.\n"
    )
    with pytest.raises(WritingValidationError) as captured:
        resolve_academic_projections(plan.encode(), _contract())
    assert captured.value.code == "WRITING_ACADEMIC_PROJECTION_AMBIGUOUS"


def test_v4_projects_realistic_global_and_chapter_sections_without_literal_bookends(
    repository_root: Path,
) -> None:
    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    contract = registry.resolve("omega_writing_production", 4)
    plan = realistic_academic_plan()
    assert b"# INTRO" not in plan and b"# CONCLUSION" not in plan

    projections = resolve_academic_projections(plan, contract.model)
    by_unit = {projection.unit_id: projection for projection in projections}
    assert len(by_unit) == 18
    assert by_unit["INTRO"].projection_version == 2
    intro = by_unit["INTRO"].content.decode("utf-8")
    assert "Perfil dos estudantes" in intro
    assert "Objetivo geral" in intro
    assert "Princípios metodológicos" in intro
    assert "Arquitetura geral do ebook" in intro

    for number in range(1, 9):
        first = by_unit[f"CH{number:02d}_A"]
        second = by_unit[f"CH{number:02d}_B"]
        assert first.content == second.content
        chapter = first.content.decode("utf-8")
        assert f"# CAPÍTULO {number} — TEMA REALISTA {number}" in chapter
        assert f"Conteúdo comprovado do capítulo {number}." in chapter
        assert "Entrada resumida" not in chapter

    conclusion = by_unit["CONCLUSION"].content.decode("utf-8")
    assert "Objetivo geral" in conclusion
    assert "Resultados de aprendizagem" in conclusion
    assert "Síntese do desenho pedagógico" in conclusion
