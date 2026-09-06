import pytest

from ebook_pipeline.visual_planning.parser import parse_visual_plan


def _figure(*, number: int = 1, page: str = "CH01-P001") -> str:
    return f"""Imagem {number} — Atenção seletiva

Página: {page}
Seção: Capítulo 1
Posição exata no texto: Após o segundo parágrafo
Conceito principal a representar: Foco
Síntese conceitual da imagem: Uma síntese
Justificativa curta: Sustenta o argumento
Objetivo da imagem: Explicar
Tipo de imagem sugerido: Diagrama
Complexidade visual: Editorial estruturada
Prompt independente para geração: Linha um
linha dois"""


def test_parser_preserves_all_v2_editorial_fields() -> None:
    result = parse_visual_plan(("\r\n" + _figure() + "\r\n").encode())

    assert result.findings == ()
    assert len(result.figures) == 1
    figure = result.figures[0]
    assert figure.number == 1
    assert figure.editorial_page == "CH01-P001"
    assert figure.section == "Capítulo 1"
    assert figure.generation_prompt == "Linha um\nlinha dois"


def test_parser_requires_exact_labels_once_and_in_order() -> None:
    raw = _figure().replace("Página:", "Pagina:").encode()

    result = parse_visual_plan(raw)

    assert result.figures == ()
    assert [finding.code for finding in result.findings] == ["VISUAL_PLAN_FIELDS_INVALID"]


@pytest.mark.parametrize(
    "raw",
    [
        _figure().replace("Seção: Capítulo 1\n", ""),
        _figure().replace("Seção: Capítulo 1", "Seção: A\nSeção: B"),
        _figure().replace(
            "Página: CH01-P001\nSeção: Capítulo 1",
            "Seção: Capítulo 1\nPágina: CH01-P001",
        ),
    ],
)
def test_parser_rejects_missing_duplicate_or_reordered_fields(raw: str) -> None:
    result = parse_visual_plan(raw.encode())

    assert result.figures == ()
    assert result.observed_figure_count == 1
    assert result.findings[0].code == "VISUAL_PLAN_FIELDS_INVALID"


def test_parser_rejects_noncanonical_complexity() -> None:
    raw = _figure().replace("Editorial estruturada", "Alta").encode()

    result = parse_visual_plan(raw)

    assert result.figures == ()
    assert result.findings[0].code == "VISUAL_PLAN_COMPLEXITY_INVALID"


def test_parser_rejects_bom_and_bare_carriage_return() -> None:
    bom = parse_visual_plan(b"\xef\xbb\xbf" + _figure().encode())
    bare_cr = parse_visual_plan(_figure().replace("\n", "\r").encode())

    assert bom.findings[0].code == "VISUAL_PLAN_BOM_FORBIDDEN"
    assert bare_cr.findings[0].code == "VISUAL_PLAN_LINE_ENDING_INVALID"


def test_parser_accepts_multiple_contiguous_figure_blocks() -> None:
    result = parse_visual_plan((_figure() + "\n\n" + _figure(number=2, page="CH01-P002")).encode())

    assert result.findings == ()
    assert [figure.number for figure in result.figures] == [1, 2]
