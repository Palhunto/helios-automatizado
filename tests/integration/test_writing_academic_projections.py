from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ServiceBundle
from test_writing_flow import _answers, _questionnaire

from ebook_pipeline.core.errors import WritingValidationError
from ebook_pipeline.writing.models import SubmissionDisposition
from ebook_pipeline.writing.repositories import PreparationRepository


def _realistic_plan() -> bytes:
    sections = (
        "# PLANEJAMENTO ACADÊMICO DO EBOOK\n\n"
        "# 1. Perfil dos estudantes\nProfissionais em formação continuada.\n\n"
        "# 2. Objetivo geral\nAplicar fundamentos com julgamento crítico.\n\n"
        "# 3. Princípios metodológicos\nFunção didática baseada em casos e análise.\n\n"
        "# 4. Arquitetura geral do ebook\nOito capítulos articulados em progressão.\n\n"
        "## Capítulo 1\nEntrada resumida que não é o recorte de produção.\n\n"
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
    return (sections + chapters).encode("utf-8")


def _ready_project(
    services: ServiceBundle, project_config_path: Path, repository_root: Path, plan: bytes
) -> str:
    project = services.projects.create(project_config_path)
    services.academic.import_questionnaire(project.id, _questionnaire(repository_root))
    services.academic.import_answers(project.id, _answers())
    services.academic.authorize_plan(project.id)
    services.academic.import_plan(project.id, plan)
    return project.id


def test_v4_context_persists_all_eager_projections_with_shared_chapter_provenance(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id = _ready_project(
        services, project_config_path, repository_root, _realistic_plan()
    )
    context = services.writing.create_context(
        project_id,
        contract_version=4,
        request_prompt_version=2,
    )
    assert context.contract_version == 4

    with services.database.connection() as connection:
        rows = connection.execute(
            "SELECT unit_id, projection_version, academic_context_sha256, artifact_id "
            "FROM writing_unit_academic_projections WHERE context_id = ? ORDER BY unit_id",
            (context.id,),
        ).fetchall()
        assert len(rows) == 18
        by_unit = {str(row["unit_id"]): row for row in rows}
        assert all(int(row["projection_version"]) == 2 for row in rows)
        for number in range(1, 9):
            assert (
                by_unit[f"CH{number:02d}_A"]["academic_context_sha256"]
                == by_unit[f"CH{number:02d}_B"]["academic_context_sha256"]
            )
        assert connection.execute(
            "SELECT count(*) FROM writing_context_projection_manifests WHERE context_id = ?",
            (context.id,),
        ).fetchone()[0] == 1

    assert services.writing.validate(project_id) == []


def test_v4_missing_late_chapter_blocks_before_context_side_effects(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    plan_without_chapter_eight = _realistic_plan().split(b"# CAP\xc3\x8dTULO 8", maxsplit=1)[0]
    project_id = _ready_project(
        services, project_config_path, repository_root, plan_without_chapter_eight
    )

    with pytest.raises(WritingValidationError) as captured:
        services.writing.create_context(
            project_id,
            contract_version=4,
            request_prompt_version=2,
        )
    assert captured.value.code == "WRITING_ACADEMIC_PROJECTION_MISSING"
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM writing_contexts WHERE project_id = ?", (project_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM writing_unit_academic_projections WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM stage_runs WHERE project_id = ? AND unit_id = 'context:create'",
            (project_id,),
        ).fetchone()[0] == 0


def test_preparation_versions_are_project_unit_monotonic_across_contexts(
    services: ServiceBundle,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    first_plan = _realistic_plan()
    project_id = _ready_project(
        services, project_config_path, repository_root, first_plan
    )
    first_context = services.writing.create_context(
        project_id,
        contract_version=4,
        request_prompt_version=2,
    )
    first_ack = services.writing.acknowledge_context_by_id(
        project_id, first_context.id, b"Primeiro contexto confirmado."
    )
    services.writing.confirm_context_by_id(
        project_id, first_context.id, first_ack.raw_version
    )
    first_intro_preparation = services.writing.prepare_unit_for_context(
        project_id, first_context.id, "INTRO"
    )
    intro_raw = ("Fundamento analítico consistente e aplicável. " * 190).encode("utf-8")
    first_intro = services.writing.import_unit_by_preparation_id(
        project_id, first_intro_preparation.id, intro_raw
    )
    assert first_intro.disposition is SubmissionDisposition.ACCEPTED
    first = services.writing.prepare_unit_for_context(
        project_id, first_context.id, "CH01_A"
    )
    _, first_request = services.writing.unit_request_by_id(project_id, first.id)

    services.academic.import_answers(project_id, _answers(" atualizada"))
    services.academic.authorize_plan(project_id)
    second_plan = first_plan.replace(
        b"Conte\xc3\xbado comprovado do cap\xc3\xadtulo 1.",
        b"Conte\xc3\xbado atualizado e comprovado do cap\xc3\xadtulo 1.",
    )
    services.academic.import_plan(project_id, second_plan)
    second_context = services.writing.create_context(
        project_id,
        contract_version=4,
        request_prompt_version=2,
    )
    second_ack = services.writing.acknowledge_context_by_id(
        project_id, second_context.id, b"Segundo contexto confirmado."
    )
    services.writing.confirm_context_by_id(
        project_id, second_context.id, second_ack.raw_version
    )
    second_intro_preparation = services.writing.prepare_unit_for_context(
        project_id, second_context.id, "INTRO"
    )
    second_intro = services.writing.import_unit_by_preparation_id(
        project_id, second_intro_preparation.id, intro_raw
    )
    assert second_intro.disposition is SubmissionDisposition.ACCEPTED
    second = services.writing.prepare_unit_for_context(
        project_id, second_context.id, "CH01_A"
    )
    repeated = services.writing.prepare_unit_for_context(
        project_id, second_context.id, "CH01_A"
    )
    _, second_request = services.writing.unit_request_by_id(project_id, second.id)

    assert first.version == 1
    assert second.version == 2
    assert repeated == second
    assert first_request != second_request
    project = services.projects.get(project_id)
    root = services.store.project_root(project.artifact_root)
    assert (
        root / "text/preparations/CH01_A/v0001/request.txt"
    ).read_bytes() == first_request
    assert (
        root / "text/preparations/CH01_A/v0002/request.txt"
    ).read_bytes() == second_request
    with services.database.connection() as connection:
        repository = PreparationRepository(connection)
        dependencies = repository.dependencies(second.id)
        assert len(dependencies) == 1
        assert dependencies[0].dependency_unit_id == "INTRO"
        assert dependencies[0].submission_id == second_intro.id
        artifact_paths = {
            str(row["relative_path"])
            for row in connection.execute(
                "SELECT relative_path FROM artifacts WHERE id IN (?, ?)",
                (second.request_artifact_id, second.manifest_artifact_id),
            ).fetchall()
        }
        assert artifact_paths == {
            "text/preparations/CH01_A/v0002/request.txt",
            "text/preparations/CH01_A/v0002/manifest.json",
        }
        assert connection.execute(
            "SELECT count(*) FROM writing_unit_preparations "
            "WHERE project_id = ? AND unit_id = 'CH01_A'",
            (project_id,),
        ).fetchone()[0] == 2
