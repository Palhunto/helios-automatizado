from pathlib import Path

import pytest

from ebook_pipeline.writing.contracts import WritingContract, WritingContractRegistry
from ebook_pipeline.writing.models import FindingSeverity, SubmissionDisposition
from ebook_pipeline.writing.rendering import render_unit_spec
from ebook_pipeline.writing.validators import validate_submission


def _contract() -> WritingContract:
    return WritingContract.model_validate(
        {
            "id": "test",
            "version": 1,
            "separator": "\n",
            "validation": {
                "allow_lists": False,
                "forbidden_phrases": ["segunda parte"],
                "linguistic_warnings": {
                    "repeated_first_word": True,
                    "repeated_initial": True,
                    "repeated_article": True,
                    "initial_run_length": 3,
                    "articles": ["o", "a"],
                },
            },
            "units": [
                {
                    "unit_id": "ONLY",
                    "order": 1,
                    "min_characters": 20,
                    "max_characters": 300,
                    "required_headings": ["Fechamento"],
                    "final_heading": "Fechamento",
                }
            ],
        }
    )


def test_objective_invariants_are_hard_errors() -> None:
    contract = _contract()
    report = validate_submission(
        b"- item proibido\n\nsegunda parte do texto", contract, contract.unit("ONLY")
    )
    assert report.disposition is SubmissionDisposition.REJECTED
    codes = {finding.code for finding in report.findings}
    assert "WRITING_LIST_SYNTAX_FORBIDDEN" in codes
    assert "WRITING_FORBIDDEN_PHRASE_PRESENT" in codes
    assert "WRITING_REQUIRED_HEADING_MISSING" in codes
    assert all(
        finding.severity is FindingSeverity.ERROR
        for finding in report.findings
        if finding.code in codes
    )


def test_linguistic_patterns_require_review_but_are_not_hard_errors() -> None:
    contract = _contract()
    raw = (
        "A análise estabelece o argumento com precisão.\n\n"
        "A abordagem amplia o argumento de forma consistente.\n\n"
        "A avaliação encerra o desenvolvimento.\n\n"
        "Fechamento\nSíntese suficientemente desenvolvida."
    ).encode()
    report = validate_submission(raw, contract, contract.unit("ONLY"))
    assert report.disposition is SubmissionDisposition.REVIEW_REQUIRED
    warnings = [item for item in report.findings if item.severity is FindingSeverity.WARNING]
    assert warnings
    assert not any(item.severity is FindingSeverity.ERROR for item in report.findings)


def test_newline_normalization_is_measurement_only() -> None:
    contract = _contract()
    raw = b"Desenvolvimento estavel.\r\n\r\nFechamento\r\nConclusao suficiente."
    report = validate_submission(raw, contract, contract.unit("ONLY"))
    assert report.character_count == len(raw.decode().replace("\r\n", "\n"))
    assert report.disposition is SubmissionDisposition.ACCEPTED


def test_invalid_utf8_forbidden_heading_and_invalid_final_heading_are_errors() -> None:
    contract = _contract()
    invalid_utf8 = validate_submission(b"\xff", contract, contract.unit("ONLY"))
    assert invalid_utf8.disposition is SubmissionDisposition.REJECTED
    assert {item.code for item in invalid_utf8.findings} >= {
        "WRITING_ENCODING_INVALID",
        "WRITING_CONTENT_EMPTY",
    }

    value = contract.model_copy(deep=True)
    value.units[0].required_headings = []
    value.units[0].final_heading = None
    value.units[0].forbidden_headings = ["Proibido"]
    forbidden = validate_submission(
        b"Texto desenvolvido com extensao suficiente.\nProibido",
        value,
        value.unit("ONLY"),
    )
    assert "WRITING_FORBIDDEN_HEADING_PRESENT" in {item.code for item in forbidden.findings}

    empty_final = validate_submission(
        b"Desenvolvimento suficientemente extenso.\nFechamento",
        contract,
        contract.unit("ONLY"),
    )
    assert "WRITING_FINAL_HEADING_INVALID" in {item.code for item in empty_final.findings}
    repeated_final = validate_submission(
        b"Fechamento\nConteudo intermediario.\nFechamento\nConteudo final.",
        contract,
        contract.unit("ONLY"),
    )
    assert "WRITING_FINAL_HEADING_INVALID" in {item.code for item in repeated_final.findings}


def test_unit_can_explicitly_allow_lists_and_disable_linguistic_warnings() -> None:
    contract = _contract()
    contract.units[0].allow_lists = True
    contract.validation.linguistic_warnings.repeated_first_word = False
    contract.validation.linguistic_warnings.repeated_initial = False
    contract.validation.linguistic_warnings.repeated_article = False
    raw = (
        "- item que o contract permite nesta unidade\n\n"
        "A análise continua.\n\nA aplicação termina.\n\n"
        "Fechamento\nSíntese conclusiva suficiente."
    ).encode()
    report = validate_submission(raw, contract, contract.unit("ONLY"))
    assert report.disposition is SubmissionDisposition.ACCEPTED


@pytest.mark.parametrize(
    ("character_count", "disposition"),
    [
        (7499, SubmissionDisposition.REJECTED),
        (7500, SubmissionDisposition.ACCEPTED),
        (8999, SubmissionDisposition.ACCEPTED),
        (9000, SubmissionDisposition.ACCEPTED),
        (10000, SubmissionDisposition.ACCEPTED),
        (10004, SubmissionDisposition.ACCEPTED),
        (11500, SubmissionDisposition.ACCEPTED),
        (11501, SubmissionDisposition.REJECTED),
    ],
)
def test_v2_uses_accepted_character_range_for_validation(
    repository_root: Path, character_count: int, disposition: SubmissionDisposition
) -> None:
    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    contract = registry.resolve("omega_writing_production", 2).model
    report = validate_submission(b"x" * character_count, contract, contract.unit("INTRO"))

    assert report.character_count == character_count
    assert report.disposition is disposition
    count_findings = [
        finding for finding in report.findings if finding.code == "WRITING_CHARACTER_COUNT_INVALID"
    ]
    assert bool(count_findings) is (disposition is SubmissionDisposition.REJECTED)
    if count_findings:
        assert count_findings[0].evidence == {
            "actual": character_count,
            "minimum": 7500,
            "maximum": 11500,
        }


def test_target_range_is_rendered_and_synthetic_ranges_are_not_hardcoded(
    repository_root: Path,
) -> None:
    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    canonical = registry.resolve("omega_writing_production", 2).model
    canonical_spec = render_unit_spec(canonical.unit("INTRO"), allow_lists=False)
    assert "caracteres_com_espacos: 9000..10000" in canonical_spec
    assert "7500..11500" not in canonical_spec

    synthetic = registry.resolve("synthetic_target_ranges", 1).model
    unit = synthetic.unit("FLEX")
    assert "caracteres_com_espacos: 40..60" in render_unit_spec(unit, allow_lists=False)
    assert (
        validate_submission(b"x" * 30, synthetic, unit).disposition
        is SubmissionDisposition.ACCEPTED
    )
    assert (
        validate_submission(b"x" * 80, synthetic, unit).disposition
        is SubmissionDisposition.ACCEPTED
    )
    assert (
        validate_submission(b"x" * 29, synthetic, unit).disposition
        is SubmissionDisposition.REJECTED
    )
    assert (
        validate_submission(b"x" * 81, synthetic, unit).disposition
        is SubmissionDisposition.REJECTED
    )
