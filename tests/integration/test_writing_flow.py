from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.academic.parsers import ANSWER_HEADER, ANSWER_TITLES
from ebook_pipeline.academic.validators import canonical_questions
from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.prompts import PromptRegistry
from ebook_pipeline.writing.models import (
    ProductionUnitStatus,
    SubmissionDisposition,
    TextUnitSubmission,
)
from ebook_pipeline.writing.repositories import CitationRepository, SubmissionRepository
from ebook_pipeline.writing.service import WritingService


def _questionnaire(repository_root: Path) -> bytes:
    prompt = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    return "\n".join(
        f"{number}. {question}"
        for number, question in enumerate(canonical_questions(prompt), start=1)
    ).encode()


def _answers(suffix: str = "") -> bytes:
    lines = [ANSWER_HEADER]
    for number, title in enumerate(ANSWER_TITLES, start=1):
        lines.extend((f"{number}. {title}", f"[CONFIRMADO] Resposta consolidada {number}{suffix}."))
    return "\n".join(lines).encode()


def _ready_project(
    services: ServiceBundle, project_config_path: Path, repository_root: Path
) -> str:
    project = services.projects.create(project_config_path)
    services.academic.import_questionnaire(project.id, _questionnaire(repository_root))
    services.academic.import_answers(project.id, _answers())
    services.academic.authorize_plan(project.id)
    services.academic.import_plan(project.id, b"# Plano academico\nDesenvolvimento aprovado.")
    return project.id


def _confirmed_context(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
    contract_id: str,
) -> str:
    project_id = _ready_project(services, project_config_path, repository_root)
    context = services.writing.create_context(project_id, contract_id=contract_id)
    acknowledgement = services.writing.acknowledge_context(
        project_id, b"Resumo em quatro linhas.\nContexto avaliado e compreendido."
    )
    confirmed = services.writing.confirm_context(project_id, acknowledgement.raw_version)
    assert confirmed.confirmed_at is not None
    return context.project_id


def _accept(
    services: ServiceBundle,
    project_id: str,
    unit_id: str,
    content: str,
    *,
    reprocess: bool = False,
) -> TextUnitSubmission:
    services.writing.prepare_unit(project_id, unit_id, reprocess=reprocess)
    submission = services.writing.import_unit(project_id, unit_id, content.encode())
    assert submission.disposition is SubmissionDisposition.ACCEPTED
    return submission


def test_context_confirmation_is_auditable_database_evidence_for_project_validation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    context = services.writing.create_context(project_id, contract_id="synthetic_demo")
    acknowledgement = services.writing.acknowledge_context(
        project_id, b"Contexto avaliado e compreendido."
    )
    services.writing.confirm_context(project_id, acknowledgement.raw_version)

    with services.database.connection() as connection:
        run = connection.execute(
            "SELECT id FROM stage_runs WHERE project_id = ? AND stage_id = ? AND unit_id = ?",
            (project_id, "writing_context_load", f"context:confirm:{acknowledgement.id}"),
        ).fetchone()
        assert run is not None
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE stage_run_id = ?", (run["id"],)
        ).fetchone()[0] == 0
    assert context.id == acknowledgement.context_id
    assert services.projects.validate(project_id) == []


def test_context_confirmation_with_invalid_run_identity_fails_project_validation(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(services, project_config_path, repository_root)
    services.writing.create_context(project_id, contract_id="synthetic_demo")
    acknowledgement = services.writing.acknowledge_context(project_id, b"Contexto avaliado.")
    services.writing.confirm_context(project_id, acknowledgement.raw_version)
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET input_hash = ? WHERE project_id = ? AND unit_id = ?",
            ("0" * 64, project_id, f"context:confirm:{acknowledgement.id}"),
        )
    assert [issue.code for issue in services.projects.validate(project_id)] == [
        "DONE_RUN_DATABASE_EVIDENCE_INVALID"
    ]


def test_complete_manual_flow_with_alternate_contract_restart_and_ledger(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    context, package = services.writing.context_package(project_id)
    assert package.count(b"Primeiro, gere um breve resumo") == 1
    assert b"{{PLANEJAMENTO_ACADEMICO_E_RESPOSTAS_CONSOLIDADAS}}" not in package
    assert b"1. Qual e" not in package  # UTF-8 canonical text was not ASCII-normalized
    assert "1. Qual é" in package.decode()

    start_preparation = services.writing.prepare_unit(project_id, "START")
    _, request = services.writing.unit_request(project_id, "START")
    assert b"Primeiro, gere um breve resumo" not in request
    assert b"Continue na mesma conversa" in request
    assert start_preparation.context_id == context.id

    start_raw = (
        "Porter (1980) fundamenta a análise, posteriormente examinada por "
        "evidências convergentes (Silva, 2020)."
    ).encode()
    start = services.writing.import_unit(project_id, "START", start_raw)
    assert start.disposition is SubmissionDisposition.ACCEPTED
    _, accepted = services.writing.unit(project_id, "START", version=1)
    assert accepted == start_raw
    with services.database.connection() as connection:
        citations = CitationRepository(connection).for_submission(start.id)
    assert [item.raw_citation_text for item in citations] == [
        "Porter (1980)",
        "(Silva, 2020)",
    ]

    body = _accept(
        services,
        project_id,
        "BODY",
        "A articulação central desenvolve o argumento com densidade e continuidade suficiente.",
    )
    end = _accept(
        services,
        project_id,
        "END",
        "Finalmente, a síntese encerra o percurso conceitual proposto.",
    )
    production_set = services.writing.production_set(project_id)
    assert production_set.complete and production_set.current
    consolidation = services.writing.consolidate(project_id)
    repeated = services.writing.consolidate(project_id)
    assert repeated == consolidation
    _, consolidated = services.writing.consolidated(project_id)
    assert (
        consolidated
        == start_raw
        + b"\n---\n"
        + services.writing.unit(project_id, "BODY", version=body.accepted_version)[1]
        + b"\n---\n"
        + services.writing.unit(project_id, "END", version=end.accepted_version)[1]
    )
    assert services.writing.validate(project_id) == []

    restarted = WritingService(
        services.config, services.database, services.store, services.academic
    )
    assert restarted.status(project_id) == services.writing.status(project_id)
    assert restarted.consolidated(project_id)[1] == consolidated


def test_dependency_currentness_propagates_and_later_failures_do_not_dethrone(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(services, project_config_path, repository_root, "synthetic_dag")
    v1 = {
        unit: _accept(services, project_id, unit, f"Conteúdo inicial válido da unidade {unit}.")
        for unit in ("A", "B", "C", "D")
    }
    assert services.writing.production_set(project_id).complete

    a2 = _accept(
        services, project_id, "A", "Conteúdo regenerado e válido da unidade A.", reprocess=True
    )
    after_a = {
        item.unit_id: item for item in services.writing.production_set(project_id).selections
    }
    assert after_a["A"].submission == a2
    assert after_a["B"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE
    assert after_a["C"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE
    assert after_a["D"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE

    b2 = _accept(
        services, project_id, "B", "Conteúdo regenerado e válido da unidade B.", reprocess=True
    )
    after_b = {
        item.unit_id: item for item in services.writing.production_set(project_id).selections
    }
    assert after_b["B"].submission == b2
    assert after_b["C"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE
    assert after_b["D"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE

    c2 = _accept(
        services, project_id, "C", "Conteúdo regenerado e válido da unidade C.", reprocess=True
    )
    after_c = {
        item.unit_id: item for item in services.writing.production_set(project_id).selections
    }
    assert after_c["C"].submission == c2
    assert after_c["D"].status is ProductionUnitStatus.HISTORICAL_INCOMPATIBLE

    d2 = _accept(
        services, project_id, "D", "Conteúdo regenerado e válido da unidade D.", reprocess=True
    )
    complete = services.writing.production_set(project_id)
    assert complete.complete
    assert {item.unit_id: item.submission for item in complete.selections}["D"] == d2

    services.writing.prepare_unit(project_id, "D", reprocess=True)
    rejected = services.writing.import_unit(project_id, "D", b"curto")
    assert rejected.disposition is SubmissionDisposition.REJECTED
    assert services.writing.production_set(project_id).selections[-1].submission == d2

    services.writing.prepare_unit(project_id, "D", reprocess=True)
    review = services.writing.import_unit(
        project_id,
        "D",
        b"Alpha apresenta desenvolvimento suficiente.\n\nAlpha conclui com outra formulacao.",
    )
    assert review.disposition is SubmissionDisposition.REVIEW_REQUIRED
    assert services.writing.production_set(project_id).selections[-1].submission == d2
    confirmed_review = services.writing.confirm_unit(project_id, "D", review.raw_version)
    assert confirmed_review.disposition is SubmissionDisposition.ACCEPTED
    assert confirmed_review.accepted_version == 3
    assert services.writing.confirm_unit(project_id, "D", review.raw_version) == confirmed_review
    assert services.writing.production_set(project_id).selections[-1].submission == confirmed_review
    assert services.writing.unit(project_id, "D", version=3)[1] == (
        b"Alpha apresenta desenvolvimento suficiente.\n\nAlpha conclui com outra formulacao."
    )
    with pytest.raises(ConflictError):
        services.writing.confirm_unit(project_id, "D", rejected.raw_version)
    with pytest.raises(NotFoundError):
        services.writing.confirm_unit(project_id, "D", 999)
    with services.database.connection() as connection:
        accepted = SubmissionRepository(connection).accepted_for_unit(complete.context_id, "A")
    assert [item.accepted_version for item in accepted] == [2, 1]
    assert v1["A"] in accepted


def test_complete_historical_set_is_preserved_but_not_current_after_m1_change(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _confirmed_context(
        services, project_config_path, repository_root, "synthetic_demo"
    )
    for unit, content in (
        ("START", "Fundamentos iniciais desenvolvidos com extensão válida."),
        ("BODY", "Desenvolvimento central consistente e suficientemente detalhado para teste."),
        ("END", "Síntese final preservada e válida."),
    ):
        _accept(services, project_id, unit, content)
    historical = services.writing.consolidate(project_id)

    services.academic.import_answers(project_id, _answers(" atualizada"))
    services.academic.authorize_plan(project_id)
    services.academic.import_plan(
        project_id, "# Plano academico V2\nNova versão aprovada.".encode()
    )
    production_set = services.writing.production_set(project_id)
    assert production_set.complete
    assert not production_set.context_current
    assert not production_set.current
    assert services.writing.consolidated(project_id)[0] == historical
    with pytest.raises(ConflictError) as captured:
        services.writing.consolidate(project_id)
    assert captured.value.code == "WRITING_CONTEXT_STALE"
