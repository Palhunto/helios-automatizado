from pathlib import Path

import pytest

from ebook_pipeline.academic.models import WordingStatus
from ebook_pipeline.academic.validators import (
    canonical_questions,
    confirm_questionnaire,
    validate_academic_plan,
    validate_questionnaire,
)
from ebook_pipeline.core.errors import AcademicValidationError, IntegrityError
from ebook_pipeline.prompts import PromptRegistry, ResolvedPrompt


def _prompt(repository_root: Path):  # type: ignore[no-untyped-def]
    return PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )


def _questionnaire(repository_root: Path) -> bytes:
    prompt = _prompt(repository_root)
    return "\n".join(
        f"{number}. {text}" for number, text in enumerate(canonical_questions(prompt), start=1)
    ).encode()


def test_questionnaire_accepts_exact_and_normalized_observed_text(
    repository_root: Path,
) -> None:
    prompt = _prompt(repository_root)
    exact = validate_questionnaire(_questionnaire(repository_root), prompt)
    assert all(
        question.wording_status is WordingStatus.NORMALIZED_MATCH for question in exact.questions
    )
    varied = _questionnaire(repository_root).decode().replace("Qual é", "  QUAL\tÉ", 1)
    normalized = validate_questionnaire(varied.encode(), prompt)
    assert normalized.questions[0].observed_text.startswith("QUAL\tÉ")
    assert normalized.questions[0].wording_status is WordingStatus.NORMALIZED_MATCH


def test_real_unnumbered_questionnaire_maps_positionally_and_is_accepted(
    repository_root: Path,
) -> None:
    prompt = _prompt(repository_root)
    raw = (
        repository_root / "tests" / "fixtures" / "m1_real_unnumbered_questionnaire.txt"
    ).read_bytes()
    result = validate_questionnaire(raw, prompt)
    assert tuple(question.question_number for question in result.questions) == (1, 2, 3, 4, 5)
    assert tuple(question.observed_text for question in result.questions) == canonical_questions(
        prompt
    )
    assert all(
        question.wording_status is WordingStatus.NORMALIZED_MATCH for question in result.questions
    )


def test_unnumbered_wording_divergence_preserves_observed_text_for_review(
    repository_root: Path,
) -> None:
    prompt = _prompt(repository_root)
    observed = list(canonical_questions(prompt))
    observed[1] = "Qual é o perfil esperado dos alunos desta disciplina?"
    result = validate_questionnaire("\n".join(observed).encode(), prompt)
    assert result.questions[1].observed_text == observed[1]
    assert result.questions[1].canonical_text != observed[1]
    assert result.questions[1].wording_status is WordingStatus.REVIEW_REQUIRED


def test_unnumbered_multiline_blocks_are_mapped_by_position(repository_root: Path) -> None:
    prompt = _prompt(repository_root)
    blocks = [
        f"{question}\nContinuação {number}."
        for number, question in enumerate(canonical_questions(prompt), start=1)
    ]
    result = validate_questionnaire("\n\n".join(blocks).encode(), prompt)
    assert tuple(question.question_number for question in result.questions) == (1, 2, 3, 4, 5)
    assert all(
        question.wording_status is WordingStatus.REVIEW_REQUIRED for question in result.questions
    )


def test_questionnaire_structural_paraphrase_requires_review(repository_root: Path) -> None:
    prompt = _prompt(repository_root)
    paraphrase = (
        _questionnaire(repository_root)
        .decode()
        .replace(
            canonical_questions(prompt)[1], "Qual é o perfil esperado dos alunos desta disciplina?"
        )
    )
    result = validate_questionnaire(paraphrase.encode(), prompt)
    assert result.questions[1].wording_status is WordingStatus.REVIEW_REQUIRED
    assert result.questions[1].observed_text != result.questions[1].canonical_text


@pytest.mark.parametrize(
    "raw",
    [
        b"1. A\n2. B\n3. C\n4. D",
        b"1. A\n2. B\n2. C\n4. D\n5. E",
        b"1. A\n3. B\n2. C\n4. D\n5. E",
        b"1. A\n2. B\n3. C\n4. D\n5.",
        b"Introduction\n1. A\n2. B\n3. C\n4. D\n5. E",
        b"A\nB\nC\nD\nE\nF",
    ],
)
def test_questionnaire_rejects_invalid_structure(repository_root: Path, raw: bytes) -> None:
    with pytest.raises(AcademicValidationError):
        validate_questionnaire(raw, _prompt(repository_root))


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\xff", "PROMPT_ENCODING_INVALID"),
        (b"A\nB\nC\nD\nE\nOUTRA SECAO", "PROMPT_QUESTIONNAIRE_CONTRACT_INVALID"),
        (b"A\nB\n\nD\nE\nREGRA DE ABERTURA", "PROMPT_QUESTIONNAIRE_CONTRACT_INVALID"),
    ],
)
def test_canonical_question_contract_rejects_corrupt_prompt(
    tmp_path: Path, content: bytes, code: str
) -> None:
    prompt = ResolvedPrompt("test", 1, "0" * 64, tmp_path / "prompt.txt", content)
    with pytest.raises(IntegrityError) as captured:
        canonical_questions(prompt)
    assert captured.value.code == code


def test_review_confirmation_requires_actual_wording_divergence(repository_root: Path) -> None:
    questionnaire = validate_questionnaire(
        _questionnaire(repository_root), _prompt(repository_root)
    )
    with pytest.raises(AcademicValidationError) as captured:
        confirm_questionnaire(questionnaire)
    assert captured.value.code == "QUESTIONNAIRE_REVIEW_NOT_REQUIRED"


@pytest.mark.parametrize("raw", [b"", b" \r\n\t", b"\xff"])
def test_academic_plan_requires_nonempty_utf8(raw: bytes) -> None:
    with pytest.raises(AcademicValidationError):
        validate_academic_plan(raw)
