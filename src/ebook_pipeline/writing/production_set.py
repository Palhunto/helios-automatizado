from __future__ import annotations

import sqlite3

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import canonical_hash
from ebook_pipeline.writing.contracts import ResolvedWritingContract
from ebook_pipeline.writing.models import (
    ProductionSelection,
    ProductionUnitStatus,
    TextProductionSet,
    TextUnitSubmission,
)
from ebook_pipeline.writing.repositories import (
    PreparationRepository,
    SubmissionRepository,
    WritingArtifactRepository,
)


class ProductionSetResolver:
    def resolve(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        context_id: str,
        contract: ResolvedWritingContract,
        context_current: bool,
    ) -> TextProductionSet:
        submissions = SubmissionRepository(connection)
        preparations = PreparationRepository(connection)
        artifacts = WritingArtifactRepository(connection)
        selected: dict[str, tuple[TextUnitSubmission, str, str]] = {}
        selections: dict[str, ProductionSelection] = {}

        for unit in contract.model.topological_units():
            candidates = submissions.accepted_for_unit(context_id, unit.unit_id)
            chosen: TextUnitSubmission | None = None
            chosen_artifact_id: str | None = None
            chosen_sha256: str | None = None
            for candidate in candidates:
                if candidate.accepted_artifact_id is None or candidate.accepted_version is None:
                    raise IntegrityError(
                        "WRITING_ACCEPTED_PROVENANCE_INCOMPLETE",
                        f"Accepted submission {candidate.id} has incomplete provenance",
                    )
                dependencies = preparations.dependencies(candidate.preparation_id)
                actual = {
                    dependency.dependency_unit_id: (
                        dependency.submission_id,
                        dependency.accepted_version,
                        dependency.artifact_id,
                        dependency.sha256,
                    )
                    for dependency in dependencies
                }
                expected: dict[str, tuple[str, int, str, str]] = {}
                compatible = True
                for dependency_id in unit.continuity_from:
                    dependency = selected.get(dependency_id)
                    if dependency is None:
                        compatible = False
                        break
                    dependency_submission, dependency_artifact, dependency_sha = dependency
                    assert dependency_submission.accepted_version is not None
                    expected[dependency_id] = (
                        dependency_submission.id,
                        dependency_submission.accepted_version,
                        dependency_artifact,
                        dependency_sha,
                    )
                if compatible and actual == expected:
                    artifact = artifacts.get(candidate.accepted_artifact_id)
                    if artifact.project_id != project_id or artifact.sha256 != candidate.raw_sha256:
                        raise IntegrityError(
                            "WRITING_ACCEPTED_ARTIFACT_INVALID",
                            f"Accepted artifact for {candidate.id} does not match its submission",
                        )
                    chosen = candidate
                    chosen_artifact_id = artifact.id
                    chosen_sha256 = artifact.sha256
                    break

            if chosen is None:
                status = (
                    ProductionUnitStatus.HISTORICAL_INCOMPATIBLE
                    if candidates
                    else ProductionUnitStatus.MISSING_ACCEPTED
                )
            else:
                status = ProductionUnitStatus.CURRENT_COMPATIBLE
                assert chosen_artifact_id is not None and chosen_sha256 is not None
                selected[unit.unit_id] = (chosen, chosen_artifact_id, chosen_sha256)
            selections[unit.unit_id] = ProductionSelection(
                unit.unit_id, status, chosen, chosen_artifact_id, chosen_sha256
            )

        ordered = tuple(selections[unit.unit_id] for unit in contract.model.ordered_units())
        complete = all(
            selection.status is ProductionUnitStatus.CURRENT_COMPATIBLE for selection in ordered
        )
        set_hash = None
        if complete:
            set_hash = canonical_hash(
                {
                    "context_id": context_id,
                    "contract": {
                        "id": contract.id,
                        "sha256": contract.sha256,
                        "version": contract.version,
                    },
                    "project_id": project_id,
                    "units": [
                        {
                            "accepted_version": selection.submission.accepted_version,
                            "artifact_id": selection.accepted_artifact_id,
                            "sha256": selection.accepted_sha256,
                            "submission_id": selection.submission.id,
                            "unit_id": selection.unit_id,
                        }
                        for selection in ordered
                        if selection.submission is not None
                    ],
                }
            )
        return TextProductionSet(
            project_id,
            context_id,
            ordered,
            complete,
            context_current,
            complete and context_current,
            set_hash,
        )
