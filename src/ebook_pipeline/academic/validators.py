from __future__ import annotations

from dataclasses import asdict, replace

from ebook_pipeline.academic.models import (
    ConsolidatedAnswers,
    ObservedQuestion,
    Questionnaire,
    WordingStatus,
)
from ebook_pipeline.academic.parsers import (
    decode_academic_text,
    normalize_comparison,
    parse_numbered_entries,
)
from ebook_pipeline.core.errors import AcademicValidationError, IntegrityError
from ebook_pipeline.core.hashing import canonical_json_bytes
from ebook_pipeline.prompts import ResolvedPrompt


def canonical_questions(prompt: ResolvedPrompt) -> tuple[str, ...]:
    try:
        text = prompt.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(
            "PROMPT_ENCODING_INVALID", f"Prompt {prompt.id}@{prompt.version} is not UTF-8"
        ) from exc
    lines = text.splitlines()
    if len(lines) < 6 or lines[5].strip() != "REGRA DE ABERTURA":
        raise IntegrityError(
            "PROMPT_QUESTIONNAIRE_CONTRACT_INVALID",
            "Academic prompt does not begin with five questions followed by REGRA DE ABERTURA",
        )
    questions = tuple(line.strip() for line in lines[:5])
    if any(not question for question in questions):
        raise IntegrityError(
            "PROMPT_QUESTIONNAIRE_CONTRACT_INVALID", "Canonical question cannot be empty"
        )
    return questions


def validate_questionnaire(raw: bytes, prompt: ResolvedPrompt) -> Questionnaire:
    observed = parse_numbered_entries(decode_academic_text(raw))
    canonical = canonical_questions(prompt)
    questions = tuple(
        ObservedQuestion(
            question_number=number,
            canonical_question_number=number,
            canonical_text=canonical[number - 1],
            observed_text=text,
            wording_status=(
                WordingStatus.NORMALIZED_MATCH
                if normalize_comparison(text) == normalize_comparison(canonical[number - 1])
                else WordingStatus.REVIEW_REQUIRED
            ),
        )
        for number, text in observed
    )
    return Questionnaire(questions)


def confirm_questionnaire(questionnaire: Questionnaire) -> Questionnaire:
    if not any(
        question.wording_status is WordingStatus.REVIEW_REQUIRED
        for question in questionnaire.questions
    ):
        raise AcademicValidationError(
            "QUESTIONNAIRE_REVIEW_NOT_REQUIRED",
            "Questionnaire has no wording divergence requiring confirmation",
        )
    return Questionnaire(
        tuple(
            replace(
                question,
                wording_status=(
                    WordingStatus.REVIEW_CONFIRMED
                    if question.wording_status is WordingStatus.REVIEW_REQUIRED
                    else question.wording_status
                ),
            )
            for question in questionnaire.questions
        )
    )


def questionnaire_bytes(
    project_id: str, version: int, questionnaire: Questionnaire, prompt: ResolvedPrompt
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "project_id": project_id,
                "version": version,
                "prompt": {
                    "id": prompt.id,
                    "version": prompt.version,
                    "sha256": prompt.sha256,
                },
                "questions": [asdict(question) for question in questionnaire.questions],
            }
        )
        + b"\n"
    )


def consolidated_answers_bytes(
    project_id: str, version: int, answers: ConsolidatedAnswers
) -> bytes:
    return (
        canonical_json_bytes(
            {
                "project_id": project_id,
                "version": version,
                "header_status": answers.header_status.value,
                "answers": [
                    {
                        "question_number": answer.question_number,
                        "blocks": [
                            {
                                "status": block.status.value,
                                "text": block.text,
                                "source_order": block.source_order,
                            }
                            for block in answer.blocks
                        ],
                    }
                    for answer in answers.answers
                ],
            }
        )
        + b"\n"
    )


def validate_academic_plan(raw: bytes) -> bytes:
    text = decode_academic_text(raw)
    if not text.strip():
        raise AcademicValidationError(
            "ACADEMIC_PLAN_EMPTY", "Academic Plan must contain non-whitespace text"
        )
    return raw
