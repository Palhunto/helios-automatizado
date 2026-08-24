from __future__ import annotations

from ebook_pipeline.academic.models import ConsolidatedAnswers
from ebook_pipeline.core.errors import IntegrityError


def render_answers_for_writing(
    answers: ConsolidatedAnswers, canonical_questions: tuple[str, ...]
) -> bytes:
    if len(canonical_questions) != 5 or len(answers.answers) != 5:
        raise IntegrityError(
            "ACADEMIC_WRITING_RENDER_INPUT_INVALID",
            "Writing input requires exactly five canonical questions and answers",
        )
    lines = ["RESPOSTAS CONSOLIDADAS PARA O PLANEJAMENTO DO EBOOK"]
    for expected_number, answer in enumerate(answers.answers, start=1):
        if answer.question_number != expected_number:
            raise IntegrityError(
                "ACADEMIC_WRITING_RENDER_ORDER_INVALID",
                "Consolidated Answers are not in canonical order",
            )
        lines.extend(("", f"{expected_number}. {canonical_questions[expected_number - 1]}"))
        for block in sorted(answer.blocks, key=lambda item: item.source_order):
            lines.append(f"[{block.status.value}] {block.text}")
    return ("\n".join(lines) + "\n").encode("utf-8")
