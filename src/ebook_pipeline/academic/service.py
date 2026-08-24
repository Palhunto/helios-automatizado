from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ebook_pipeline.academic.models import (
    AcademicDocument,
    AcademicPlanAuthorization,
    AcceptanceStatus,
    AcceptedAcademicArtifact,
    ConsolidatedAnswers,
    CurrentWritingInputs,
    DocumentKind,
    EditorialMarker,
    WordingStatus,
)
from ebook_pipeline.academic.parsers import decode_academic_text, parse_consolidated_answers
from ebook_pipeline.academic.rendering import render_answers_for_writing
from ebook_pipeline.academic.repositories import (
    AcademicArtifactRepository,
    AcademicDocumentRepository,
    AuthorizationRepository,
    ReviewDecisionRepository,
)
from ebook_pipeline.academic.validators import (
    canonical_questions,
    confirm_questionnaire,
    consolidated_answers_bytes,
    questionnaire_bytes,
    validate_academic_plan,
    validate_questionnaire,
)
from ebook_pipeline.config import AppConfig, load_project_config
from ebook_pipeline.core.errors import (
    AcademicValidationError,
    ConflictError,
    HeliosError,
    IntegrityError,
    NotFoundError,
)
from ebook_pipeline.core.hashing import canonical_hash, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import (
    Artifact,
    Project,
    RecoveryResult,
    RunStatus,
    StageRun,
    StoredFile,
    ValidationIssue,
)
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.prompts import PromptRegistry, ResolvedPrompt
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)

QUESTIONNAIRE_STAGE = "academic_questionnaire"
ANSWERS_STAGE = "consolidated_answers"
PLAN_STAGE = "academic_plan"


class AcademicService:
    def __init__(self, config: AppConfig, database: Database, store: ArtifactStore) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.registry = PromptRegistry(config.prompt_registry)
        self.state_machine = StateMachine()

    def import_questionnaire(self, project_id: str, raw: bytes) -> AcademicDocument:
        with self.database.connection() as connection:
            project, prompt = self._context(connection, project_id)
            input_hash = self._input_hash("questionnaire.import", raw, prompt)
            document, run, already_complete = self._begin_import(
                connection,
                project,
                DocumentKind.QUESTIONNAIRE,
                QUESTIONNAIRE_STAGE,
                input_hash,
                sha256_bytes(raw),
                prompt,
            )
            if already_complete:
                return document
            return self._materialize_questionnaire(connection, project, prompt, document, run, raw)

    def confirm_questionnaire(self, project_id: str, raw_version: int) -> AcademicDocument:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            documents = AcademicDocumentRepository(connection)
            document = documents.by_raw_version(project_id, DocumentKind.QUESTIONNAIRE, raw_version)
            if document is None:
                raise NotFoundError(
                    "QUESTIONNAIRE_VERSION_NOT_FOUND",
                    f"Questionnaire raw version {raw_version} was not found",
                )
            if document.acceptance_status is AcceptanceStatus.ACCEPTED:
                return document
            if document.acceptance_status is not AcceptanceStatus.REVIEW_REQUIRED:
                raise ConflictError(
                    "QUESTIONNAIRE_NOT_REVIEWABLE",
                    f"Questionnaire is {document.acceptance_status.value}, not review_required",
                )
            prompt = self.registry.resolve(document.prompt_id, document.prompt_version)
            if prompt.sha256 != document.prompt_sha256:
                raise IntegrityError(
                    "PROMPT_HASH_MISMATCH", "Questionnaire prompt binding no longer matches"
                )
            raw_content = self._artifact_content(connection, project, document.raw_artifact_id)
            confirmed = confirm_questionnaire(validate_questionnaire(raw_content, prompt))
            input_hash = canonical_hash(
                {
                    "decision": "confirm_observed_questionnaire",
                    "document_id": document.id,
                    "raw_sha256": document.raw_sha256,
                }
            )
            unit_id = f"confirm:{document.id}"
            runs = StageRunRepository(connection)
            existing = runs.find_identity(
                project.id, QUESTIONNAIRE_STAGE, unit_id, input_hash, version=1
            )
            if existing is not None:
                if existing.status is RunStatus.DONE:
                    return documents.get(document.id)
                if existing.status is RunStatus.RUNNING:
                    raise IntegrityError(
                        "ACADEMIC_RECOVERY_REQUIRED",
                        "Questionnaire confirmation was interrupted; recover first",
                        recoverable=True,
                    )
                run = self._resume_run(connection, existing)
            else:
                run = self.state_machine.new_run(
                    project_id=project.id,
                    stage_id=QUESTIONNAIRE_STAGE,
                    unit_id=unit_id,
                    input_hash=input_hash,
                    max_attempts=self.config.max_attempts,
                )
                with self.database.transaction(connection):
                    runs.add(run)
                    running = self.state_machine.transition(run, RunStatus.RUNNING)
                    runs.update(running, expected_status=run.status)
                run = running
            accepted_version = self._next_accepted_version(
                connection, project.id, DocumentKind.QUESTIONNAIRE
            )
            content = questionnaire_bytes(project.id, accepted_version, confirmed, prompt)
            try:
                stored = self.store.write_bytes(
                    project.artifact_root,
                    self._accepted_path(DocumentKind.QUESTIONNAIRE, document.raw_version),
                    content,
                )
                return self._finalize_confirmation(
                    connection, project, document, run, stored, accepted_version
                )
            except HeliosError as exc:
                self._record_operational_failure(connection, run.id, exc)
                raise
            except Exception as exc:
                error = IntegrityError(
                    "QUESTIONNAIRE_CONFIRM_FAILED",
                    f"Unexpected questionnaire confirmation failure: {exc}",
                    recoverable=True,
                )
                self._record_operational_failure(connection, run.id, error)
                raise error from exc

    def import_answers(self, project_id: str, raw: bytes) -> AcademicDocument:
        with self.database.connection() as connection:
            project, prompt = self._context(connection, project_id)
            upstream = AcademicDocumentRepository(connection).current_accepted(
                project.id, DocumentKind.QUESTIONNAIRE
            )
            if upstream is None:
                raise ConflictError(
                    "QUESTIONNAIRE_NOT_ACCEPTED",
                    "An accepted questionnaire is required before consolidated answers",
                )
            input_hash = self._input_hash(
                "answers.import", raw, prompt, upstream_document_id=upstream.id
            )
            document, run, already_complete = self._begin_import(
                connection,
                project,
                DocumentKind.CONSOLIDATED_ANSWERS,
                ANSWERS_STAGE,
                input_hash,
                sha256_bytes(raw),
                prompt,
                upstream_document_id=upstream.id,
            )
            if already_complete:
                return document
            return self._materialize_answers(connection, project, prompt, document, run, raw)

    def authorize_plan(self, project_id: str) -> AcademicPlanAuthorization:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            document = AcademicDocumentRepository(connection).current_accepted(
                project_id, DocumentKind.CONSOLIDATED_ANSWERS
            )
            if document is None:
                raise ConflictError(
                    "ANSWERS_NOT_ACCEPTED", "Accepted consolidated answers are required"
                )
            answers = self._load_answers(connection, document)
            conflict_count = sum(
                block.status is EditorialMarker.CONFLITO
                for answer in answers.answers
                for block in answer.blocks
            )
            if conflict_count:
                raise ConflictError(
                    "ACADEMIC_CONFLICT_BLOCKS_AUTHORIZATION",
                    f"Current Answers contain {conflict_count} CONFLITO marker(s)",
                    evidence={"conflict_count": conflict_count},
                )
            with self.database.transaction(connection):
                return AuthorizationRepository(connection).add(project_id, document.id)

    def import_plan(self, project_id: str, raw: bytes) -> AcademicDocument:
        with self.database.connection() as connection:
            project, prompt = self._context(connection, project_id)
            documents = AcademicDocumentRepository(connection)
            answers = documents.current_accepted(project.id, DocumentKind.CONSOLIDATED_ANSWERS)
            if answers is None:
                raise ConflictError(
                    "ANSWERS_NOT_ACCEPTED", "Accepted consolidated answers are required"
                )
            authorization = AuthorizationRepository(connection).get_for_answers(answers.id)
            if authorization is None:
                raise ConflictError(
                    "ACADEMIC_PLAN_NOT_AUTHORIZED",
                    "Current consolidated answers have no planning authorization",
                )
            input_hash = self._input_hash(
                "plan.import",
                raw,
                prompt,
                upstream_document_id=answers.id,
                authorization_id=authorization.id,
            )
            document, run, already_complete = self._begin_import(
                connection,
                project,
                DocumentKind.ACADEMIC_PLAN,
                PLAN_STAGE,
                input_hash,
                sha256_bytes(raw),
                prompt,
                upstream_document_id=answers.id,
                authorization_id=authorization.id,
            )
            if already_complete:
                return document
            return self._materialize_plan(connection, project, prompt, document, run, raw)

    def get_current_answers(self, project_id: str) -> AcceptedAcademicArtifact | None:
        return self._current_view(project_id, DocumentKind.CONSOLIDATED_ANSWERS)

    def get_current_compatible_academic_plan(
        self, project_id: str
    ) -> AcceptedAcademicArtifact | None:
        with self.database.connection() as connection:
            documents = AcademicDocumentRepository(connection)
            answers = documents.current_accepted(project_id, DocumentKind.CONSOLIDATED_ANSWERS)
            if answers is None:
                return None
            rows = [
                document
                for document in documents.list_for_project(project_id)
                if document.document_kind is DocumentKind.ACADEMIC_PLAN
                and document.acceptance_status is AcceptanceStatus.ACCEPTED
                and document.upstream_document_id == answers.id
            ]
            if not rows:
                return None
            document = max(rows, key=lambda item: item.accepted_version or 0)
            return self._view(connection, document)

    def get_current_writing_inputs(self, project_id: str) -> CurrentWritingInputs | None:
        """Return a transactionally consistent M1 snapshot for WritingContext creation."""
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                documents = AcademicDocumentRepository(connection)
                answers_document = documents.current_accepted(
                    project_id, DocumentKind.CONSOLIDATED_ANSWERS
                )
                if answers_document is None:
                    connection.commit()
                    return None
                candidates = [
                    document
                    for document in documents.list_for_project(project_id)
                    if document.document_kind is DocumentKind.ACADEMIC_PLAN
                    and document.acceptance_status is AcceptanceStatus.ACCEPTED
                    and document.upstream_document_id == answers_document.id
                ]
                if not candidates:
                    connection.commit()
                    return None
                plan_document = max(candidates, key=lambda item: item.accepted_version or 0)
                if plan_document.upstream_document_id != answers_document.id:
                    raise IntegrityError(
                        "ACADEMIC_PLAN_PROVENANCE_INVALID",
                        "Academic Plan does not derive from the selected Consolidated Answers",
                    )
                answers_view = self._view(connection, answers_document)
                plan_view = self._view(connection, plan_document)
                prompt = self.registry.resolve(
                    answers_document.prompt_id, answers_document.prompt_version
                )
                if prompt.sha256 != answers_document.prompt_sha256:
                    raise IntegrityError(
                        "PROMPT_HASH_MISMATCH",
                        "Consolidated Answers prompt binding no longer matches",
                    )
                parsed = self._load_answers(connection, answers_document)
                rendered = render_answers_for_writing(parsed, canonical_questions(prompt))
                connection.commit()
                return CurrentWritingInputs(answers_view, plan_view, rendered)
            except Exception:
                connection.rollback()
                raise

    def document(
        self,
        project_id: str,
        kind: DocumentKind,
        *,
        version: int | None = None,
        raw: bool = False,
    ) -> tuple[AcademicDocument, bytes]:
        with self.database.connection() as connection:
            repository = AcademicDocumentRepository(connection)
            document = (
                repository.by_raw_version(project_id, kind, version)
                if raw and version is not None
                else repository.by_accepted_version(project_id, kind, version)
                if version is not None
                else repository.latest(project_id, kind)
                if raw
                else repository.current_accepted(project_id, kind)
            )
            if document is None:
                raise NotFoundError(
                    "ACADEMIC_DOCUMENT_NOT_FOUND", f"No {kind.value} document was found"
                )
            artifact_id = document.raw_artifact_id if raw else document.accepted_artifact_id
            project = self._project(connection, project_id)
            return document, self._artifact_content(connection, project, artifact_id)

    def status(self, project_id: str) -> dict[str, object]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            repository = AcademicDocumentRepository(connection)
            questionnaire = repository.latest(project_id, DocumentKind.QUESTIONNAIRE)
            answers = repository.current_accepted(project_id, DocumentKind.CONSOLIDATED_ANSWERS)
            current_plan = self.get_current_compatible_academic_plan(project_id)
            marker_counts = {marker.value: 0 for marker in EditorialMarker}
            authorization = None
            if answers is not None:
                parsed = self._load_answers(connection, answers)
                for answer in parsed.answers:
                    for block in answer.blocks:
                        marker_counts[block.status.value] += 1
                authorization = AuthorizationRepository(connection).get_for_answers(answers.id)
            plans = [
                document
                for document in repository.list_for_project(project_id)
                if document.document_kind is DocumentKind.ACADEMIC_PLAN
                and document.acceptance_status is AcceptanceStatus.ACCEPTED
            ]
            return {
                "project_id": project_id,
                "questionnaire": self._document_status(questionnaire),
                "consolidated_answers": {
                    **self._document_status(answers),
                    "question_count": 5 if answers is not None else 0,
                    "marker_counts": marker_counts,
                },
                "planning_authorization": (
                    None
                    if authorization is None
                    else {
                        "id": authorization.id,
                        "answers_document_id": authorization.answers_document_id,
                        "authorized_at": authorization.authorized_at,
                    }
                ),
                "academic_plan": {
                    **self._document_status(
                        None if current_plan is None else current_plan.document
                    ),
                    "current": current_plan is not None,
                    "preserved_versions": len(plans),
                },
            }

    def validate(self, project_id: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            documents = AcademicDocumentRepository(connection).list_for_project(project_id)
            for document in documents:
                for label, artifact_id in (
                    ("raw", document.raw_artifact_id),
                    ("accepted", document.accepted_artifact_id),
                ):
                    if (
                        label == "accepted"
                        and document.acceptance_status is not AcceptanceStatus.ACCEPTED
                    ):
                        continue
                    if artifact_id is None:
                        issues.append(
                            ValidationIssue(
                                "ACADEMIC_ARTIFACT_REFERENCE_MISSING",
                                f"{document.document_kind.value} {label} artifact "
                                "is not registered",
                            )
                        )
                        continue
                    try:
                        artifact = AcademicArtifactRepository(connection).get(artifact_id)
                        inspected = self.store.inspect(
                            project.artifact_root, artifact.relative_path
                        )
                        if (
                            inspected.sha256 != artifact.sha256
                            or inspected.byte_size != artifact.byte_size
                        ):
                            issues.append(
                                ValidationIssue(
                                    "ACADEMIC_ARTIFACT_HASH_MISMATCH",
                                    f"Academic artifact {artifact.relative_path!r} differs",
                                    artifact.relative_path,
                                )
                            )
                    except HeliosError as exc:
                        issues.append(ValidationIssue(exc.code, exc.message))
            current_answers = AcademicDocumentRepository(connection).current_accepted(
                project_id, DocumentKind.CONSOLIDATED_ANSWERS
            )
            current_plan = self.get_current_compatible_academic_plan(project_id)
            if (
                current_plan is not None
                and current_answers is not None
                and current_plan.document.upstream_document_id != current_answers.id
            ):
                issues.append(
                    ValidationIssue(
                        "ACADEMIC_PLAN_PROVENANCE_INVALID",
                        "Current plan does not derive from current Answers",
                    )
                )
        return issues

    def recover_run(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult | None:
        if run.stage_id not in {QUESTIONNAIRE_STAGE, ANSWERS_STAGE, PLAN_STAGE}:
            return None
        if run.unit_id.startswith("confirm:"):
            return self._recover_confirmation(connection, project, run)
        documents = AcademicDocumentRepository(connection)
        document = documents.get_by_run(run.id)
        if document is None:
            return self._recovery_pending(
                connection,
                run,
                "ACADEMIC_DOCUMENT_MISSING",
                "Academic run has no operation metadata",
            )
        try:
            prompt = self.registry.resolve(document.prompt_id, document.prompt_version)
            if prompt.sha256 != document.prompt_sha256:
                raise IntegrityError(
                    "PROMPT_HASH_MISMATCH", "Academic document prompt binding differs"
                )
            prompt_stored = self.store.inspect(project.artifact_root, self._prompt_path(prompt))
            raw_stored = self.store.inspect(
                project.artifact_root,
                self._raw_path(document.document_kind, document.raw_version),
            )
            if raw_stored.sha256 != document.raw_sha256:
                raise IntegrityError(
                    "ACADEMIC_RAW_HASH_MISMATCH", "Academic raw artifact differs from its input"
                )
            raw = self.store.resolve(project.artifact_root, raw_stored.relative_path).read_bytes()
            accepted_version = self._next_accepted_version(
                connection, project.id, document.document_kind
            )
            if document.document_kind is DocumentKind.QUESTIONNAIRE:
                questionnaire = validate_questionnaire(raw, prompt)
                if any(
                    item.wording_status is WordingStatus.REVIEW_REQUIRED
                    for item in questionnaire.questions
                ):
                    reviewed = self._finish_review_required(
                        connection,
                        project,
                        prompt,
                        document,
                        run,
                        prompt_stored,
                        raw_stored,
                    )
                    return RecoveryResult(
                        run.id, run.status, RunStatus.DONE, reviewed.acceptance_status.value
                    )
                expected = questionnaire_bytes(project.id, accepted_version, questionnaire, prompt)
            elif document.document_kind is DocumentKind.CONSOLIDATED_ANSWERS:
                parsed = parse_consolidated_answers(decode_academic_text(raw))
                expected = consolidated_answers_bytes(project.id, accepted_version, parsed)
            else:
                expected = validate_academic_plan(raw)
            accepted_path = self._accepted_path(document.document_kind, document.raw_version)
            accepted_stored = self.store.inspect(project.artifact_root, accepted_path)
            if accepted_stored.sha256 != sha256_bytes(expected) or accepted_stored.byte_size != len(
                expected
            ):
                raise IntegrityError(
                    "ACADEMIC_ACCEPTED_HASH_MISMATCH",
                    "Academic accepted artifact cannot be proven from its raw input",
                )
            self._finish_accepted(
                connection,
                project,
                prompt,
                document,
                run,
                prompt_stored,
                raw_stored,
                accepted_stored,
                accepted_version,
            )
            return RecoveryResult(run.id, run.status, RunStatus.DONE, "Artifacts reconciled")
        except AcademicValidationError as exc:
            try:
                prompt = self.registry.resolve(document.prompt_id, document.prompt_version)
                prompt_stored = self.store.inspect(project.artifact_root, self._prompt_path(prompt))
                raw_stored = self.store.inspect(
                    project.artifact_root,
                    self._raw_path(document.document_kind, document.raw_version),
                )
                self._finish_rejected(
                    connection,
                    project,
                    prompt,
                    document,
                    run,
                    prompt_stored,
                    raw_stored,
                    exc,
                )
            except HeliosError as nested:
                return self._recovery_blocked(connection, run, nested.code, nested.message)
            return RecoveryResult(run.id, run.status, RunStatus.BLOCKED, exc.message)
        except HeliosError as exc:
            if exc.code in {"ARTIFACT_MISSING", "PROMPT_FILE_MISSING"}:
                return self._recovery_pending(connection, run, exc.code, exc.message)
            return self._recovery_blocked(connection, run, exc.code, exc.message)
        except (OSError, UnicodeError, ValueError) as exc:
            return self._recovery_blocked(
                connection,
                run,
                "ACADEMIC_RECOVERY_CORRUPT",
                f"Academic artifacts cannot be trusted: {exc}",
            )

    def _recover_confirmation(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        document_id = run.unit_id.removeprefix("confirm:")
        documents = AcademicDocumentRepository(connection)
        try:
            document = documents.get(document_id)
            prompt = self.registry.resolve(document.prompt_id, document.prompt_version)
            raw = self._artifact_content(connection, project, document.raw_artifact_id)
            confirmed = confirm_questionnaire(validate_questionnaire(raw, prompt))
            accepted_version = self._next_accepted_version(
                connection, project.id, DocumentKind.QUESTIONNAIRE
            )
            expected = questionnaire_bytes(project.id, accepted_version, confirmed, prompt)
            stored = self.store.inspect(
                project.artifact_root,
                self._accepted_path(DocumentKind.QUESTIONNAIRE, document.raw_version),
            )
            if stored.sha256 != sha256_bytes(expected) or stored.byte_size != len(expected):
                raise IntegrityError(
                    "ACADEMIC_ACCEPTED_HASH_MISMATCH",
                    "Confirmed questionnaire artifact differs from the expected output",
                )
            self._finalize_confirmation(
                connection, project, document, run, stored, accepted_version
            )
            return RecoveryResult(run.id, run.status, RunStatus.DONE, "Review reconciled")
        except HeliosError as exc:
            if exc.code == "ARTIFACT_MISSING":
                return self._recovery_pending(connection, run, exc.code, exc.message)
            return self._recovery_blocked(connection, run, exc.code, exc.message)
        except (OSError, UnicodeError, ValueError) as exc:
            return self._recovery_blocked(
                connection,
                run,
                "QUESTIONNAIRE_REVIEW_CORRUPT",
                f"Questionnaire review cannot be trusted: {exc}",
            )

    def _recovery_pending(
        self,
        connection: sqlite3.Connection,
        run: StageRun,
        code: str,
        message: str,
    ) -> RecoveryResult:
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=True,
            )
            retry = self.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
            runs.update(retry, expected_status=run.status)
        return RecoveryResult(run.id, run.status, retry.status, message)

    def _recovery_blocked(
        self,
        connection: sqlite3.Connection,
        run: StageRun,
        code: str,
        message: str,
    ) -> RecoveryResult:
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=False,
            )
            failed = self.state_machine.transition(run, RunStatus.FAILED)
            runs.update(failed, expected_status=run.status)
            blocked = self.state_machine.transition(failed, RunStatus.BLOCKED)
            runs.update(blocked, expected_status=failed.status)
        return RecoveryResult(run.id, run.status, blocked.status, message)

    def _materialize_questionnaire(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        raw: bytes,
    ) -> AcademicDocument:
        prompt_stored: StoredFile | None = None
        raw_stored: StoredFile | None = None
        try:
            prompt_stored = self._freeze_prompt(project, prompt)
            raw_stored = self.store.write_bytes(
                project.artifact_root,
                self._raw_path(document.document_kind, document.raw_version),
                raw,
            )
            questionnaire = validate_questionnaire(raw, prompt)
            review_required = any(
                item.wording_status is WordingStatus.REVIEW_REQUIRED
                for item in questionnaire.questions
            )
            if review_required:
                return self._finish_review_required(
                    connection, project, prompt, document, run, prompt_stored, raw_stored
                )
            accepted_version = self._next_accepted_version(
                connection, project.id, DocumentKind.QUESTIONNAIRE
            )
            accepted_content = questionnaire_bytes(
                project.id, accepted_version, questionnaire, prompt
            )
            accepted_stored = self.store.write_bytes(
                project.artifact_root,
                self._accepted_path(document.document_kind, document.raw_version),
                accepted_content,
            )
            return self._finish_accepted(
                connection,
                project,
                prompt,
                document,
                run,
                prompt_stored,
                raw_stored,
                accepted_stored,
                accepted_version,
            )
        except AcademicValidationError as exc:
            if raw_stored is not None and prompt_stored is not None:
                self._finish_rejected(
                    connection, project, prompt, document, run, prompt_stored, raw_stored, exc
                )
            else:
                self._record_operational_failure(connection, run.id, exc)
            raise
        except HeliosError as exc:
            self._record_operational_failure(connection, run.id, exc)
            raise
        except Exception as exc:
            error = IntegrityError(
                "QUESTIONNAIRE_IMPORT_FAILED",
                f"Unexpected questionnaire import failure: {exc}",
                recoverable=True,
            )
            self._record_operational_failure(connection, run.id, error)
            raise error from exc

    def _materialize_answers(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        raw: bytes,
    ) -> AcademicDocument:
        return self._materialize_content(
            connection,
            project,
            prompt,
            document,
            run,
            raw,
            lambda content, version: consolidated_answers_bytes(
                project.id,
                version,
                parse_consolidated_answers(decode_academic_text(content)),
            ),
            "ANSWERS_IMPORT_FAILED",
        )

    def _materialize_plan(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        raw: bytes,
    ) -> AcademicDocument:
        return self._materialize_content(
            connection,
            project,
            prompt,
            document,
            run,
            raw,
            lambda content, _version: validate_academic_plan(content),
            "ACADEMIC_PLAN_IMPORT_FAILED",
        )

    def _materialize_content(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        raw: bytes,
        accepted_builder: Callable[[bytes, int], bytes],
        failure_code: str,
    ) -> AcademicDocument:
        prompt_stored: StoredFile | None = None
        raw_stored: StoredFile | None = None
        try:
            prompt_stored = self._freeze_prompt(project, prompt)
            raw_stored = self.store.write_bytes(
                project.artifact_root,
                self._raw_path(document.document_kind, document.raw_version),
                raw,
            )
            accepted_version = self._next_accepted_version(
                connection, project.id, document.document_kind
            )
            accepted_content = accepted_builder(raw, accepted_version)
            accepted_stored = self.store.write_bytes(
                project.artifact_root,
                self._accepted_path(document.document_kind, document.raw_version),
                accepted_content,
            )
            return self._finish_accepted(
                connection,
                project,
                prompt,
                document,
                run,
                prompt_stored,
                raw_stored,
                accepted_stored,
                accepted_version,
            )
        except AcademicValidationError as exc:
            if raw_stored is not None and prompt_stored is not None:
                self._finish_rejected(
                    connection, project, prompt, document, run, prompt_stored, raw_stored, exc
                )
            else:
                self._record_operational_failure(connection, run.id, exc)
            raise
        except HeliosError as exc:
            self._record_operational_failure(connection, run.id, exc)
            raise
        except Exception as exc:
            error = IntegrityError(
                failure_code, f"Unexpected academic import failure: {exc}", recoverable=True
            )
            self._record_operational_failure(connection, run.id, error)
            raise error from exc

    def _begin_import(
        self,
        connection: sqlite3.Connection,
        project: Project,
        kind: DocumentKind,
        stage_id: str,
        input_hash: str,
        raw_sha256: str,
        prompt: ResolvedPrompt,
        *,
        upstream_document_id: str | None = None,
        authorization_id: str | None = None,
    ) -> tuple[AcademicDocument, StageRun, bool]:
        documents = AcademicDocumentRepository(connection)
        existing = documents.find_by_input(project.id, kind, input_hash)
        if existing is not None:
            document, run = existing
            if run.status is RunStatus.DONE:
                return document, run, True
            if run.status is RunStatus.RUNNING:
                raise IntegrityError(
                    "ACADEMIC_RECOVERY_REQUIRED",
                    f"{stage_id} was interrupted; recover the project first",
                    recoverable=True,
                )
            if run.status is RunStatus.BLOCKED:
                raise AcademicValidationError(
                    "ACADEMIC_INPUT_PREVIOUSLY_REJECTED",
                    "The same academic input was already rejected",
                )
            return document, self._resume_run(connection, run), False

        latest = documents.latest(project.id, kind)
        if latest is not None:
            latest_run = StageRunRepository(connection).get(latest.stage_run_id)
            if latest_run.status in {
                RunStatus.PENDING,
                RunStatus.RUNNING,
                RunStatus.FAILED,
                RunStatus.PENDING_RETRY,
            }:
                raise IntegrityError(
                    "ACADEMIC_RECOVERY_REQUIRED",
                    f"Previous {stage_id} run must be recovered or retried first",
                    recoverable=True,
                )
        version = 1 if latest is None else latest.raw_version + 1
        run = self.state_machine.new_run(
            project_id=project.id,
            stage_id=stage_id,
            unit_id=stage_id,
            input_hash=input_hash,
            max_attempts=self.config.max_attempts,
            version=version,
            supersedes_run_id=None if latest is None else latest.stage_run_id,
        )
        document = AcademicDocument(
            id=new_id(),
            project_id=project.id,
            stage_run_id=run.id,
            document_kind=kind,
            acceptance_status=AcceptanceStatus.PROCESSING,
            raw_version=version,
            accepted_version=None,
            raw_sha256=raw_sha256,
            raw_artifact_id=None,
            accepted_artifact_id=None,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            upstream_document_id=upstream_document_id,
            authorization_id=authorization_id,
            created_at=utc_now(),
            accepted_at=None,
        )
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            runs.add(run)
            documents.add(document)
            running = self.state_machine.transition(run, RunStatus.RUNNING)
            runs.update(running, expected_status=run.status)
        return document, running, False

    def _resume_run(self, connection: sqlite3.Connection, run: StageRun) -> StageRun:
        runs = StageRunRepository(connection)
        current = run
        with self.database.transaction(connection):
            if current.status is RunStatus.FAILED:
                target = (
                    RunStatus.PENDING_RETRY
                    if current.attempt < current.max_attempts
                    else RunStatus.BLOCKED
                )
                current = self.state_machine.transition(current, target)
                runs.update(current, expected_status=run.status)
            if current.status is RunStatus.BLOCKED:
                raise ConflictError("ACADEMIC_RUN_BLOCKED", "Academic run is blocked")
            running = self.state_machine.transition(current, RunStatus.RUNNING)
            runs.update(running, expected_status=current.status)
        return running

    def _finish_review_required(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        prompt_stored: StoredFile,
        raw_stored: StoredFile,
    ) -> AcademicDocument:
        documents = AcademicDocumentRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            self._ensure_prompt_artifact(connection, project, run, prompt, prompt_stored)
            raw_artifact = self._register_artifact(
                connection,
                project,
                run,
                f"academic_{document.document_kind.value}_raw",
                raw_stored,
                document.raw_version,
            )
            current = documents.get(document.id)
            current = documents.set_raw_artifact(current, raw_artifact.id)
            reviewed = documents.set_disposition(current, AcceptanceStatus.REVIEW_REQUIRED)
            current_run = runs.get(run.id)
            done = self.state_machine.transition(current_run, RunStatus.DONE)
            runs.update(done, expected_status=current_run.status)
        return reviewed

    def _finalize_confirmation(
        self,
        connection: sqlite3.Connection,
        project: Project,
        document: AcademicDocument,
        run: StageRun,
        stored: StoredFile,
        accepted_version: int,
    ) -> AcademicDocument:
        documents = AcademicDocumentRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            artifact = self._register_artifact(
                connection,
                project,
                run,
                "academic_questionnaire_accepted",
                stored,
                accepted_version,
            )
            current = documents.get(document.id)
            accepted = documents.accept(current, artifact.id, accepted_version)
            ReviewDecisionRepository(connection).add(project.id, document.id, run.id)
            current_run = runs.get(run.id)
            done = self.state_machine.transition(current_run, RunStatus.DONE)
            runs.update(done, expected_status=current_run.status)
        return accepted

    def _finish_accepted(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        prompt_stored: StoredFile,
        raw_stored: StoredFile,
        accepted_stored: StoredFile,
        accepted_version: int,
    ) -> AcademicDocument:
        documents = AcademicDocumentRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            self._ensure_prompt_artifact(connection, project, run, prompt, prompt_stored)
            raw_artifact = self._register_artifact(
                connection,
                project,
                run,
                f"academic_{document.document_kind.value}_raw",
                raw_stored,
                document.raw_version,
            )
            accepted_artifact = self._register_artifact(
                connection,
                project,
                run,
                f"academic_{document.document_kind.value}_accepted",
                accepted_stored,
                accepted_version,
            )
            current = documents.get(document.id)
            current = documents.set_raw_artifact(current, raw_artifact.id)
            accepted = documents.accept(current, accepted_artifact.id, accepted_version)
            current_run = runs.get(run.id)
            done = self.state_machine.transition(current_run, RunStatus.DONE)
            runs.update(done, expected_status=current_run.status)
        return accepted

    def _finish_rejected(
        self,
        connection: sqlite3.Connection,
        project: Project,
        prompt: ResolvedPrompt,
        document: AcademicDocument,
        run: StageRun,
        prompt_stored: StoredFile,
        raw_stored: StoredFile,
        error: AcademicValidationError,
    ) -> None:
        documents = AcademicDocumentRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            self._ensure_prompt_artifact(connection, project, run, prompt, prompt_stored)
            raw_artifact = self._register_artifact(
                connection,
                project,
                run,
                f"academic_{document.document_kind.value}_raw",
                raw_stored,
                document.raw_version,
            )
            current = documents.get(document.id)
            current = documents.set_raw_artifact(current, raw_artifact.id)
            documents.set_disposition(current, AcceptanceStatus.REJECTED)
            ErrorRepository(connection).add(
                project_id=project.id,
                stage_run_id=run.id,
                code=error.code,
                message=error.message,
                recoverable=False,
                evidence=error.context.evidence,
            )
            current_run = runs.get(run.id)
            failed = self.state_machine.transition(current_run, RunStatus.FAILED)
            runs.update(failed, expected_status=current_run.status)
            blocked = self.state_machine.transition(failed, RunStatus.BLOCKED)
            runs.update(blocked, expected_status=failed.status)

    def _record_operational_failure(
        self, connection: sqlite3.Connection, run_id: str, error: HeliosError
    ) -> None:
        runs = StageRunRepository(connection)
        current = runs.get(run_id)
        if current.status is not RunStatus.RUNNING:
            return
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=current.project_id,
                stage_run_id=current.id,
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                evidence=error.context.evidence,
            )
            failed = self.state_machine.transition(current, RunStatus.FAILED)
            runs.update(failed, expected_status=current.status)
            target = (
                RunStatus.PENDING_RETRY
                if error.recoverable and failed.attempt < failed.max_attempts
                else RunStatus.BLOCKED
            )
            final = self.state_machine.transition(failed, target)
            runs.update(final, expected_status=failed.status)

    def _context(
        self, connection: sqlite3.Connection, project_id: str
    ) -> tuple[Project, ResolvedPrompt]:
        project = ProjectRepository(connection).get(project_id)
        config_path = self.store.resolve(project.artifact_root, project.config_path)
        loaded = load_project_config(config_path)
        reference = loaded.model.prompts.academic_planning
        return project, self.registry.resolve(reference.id, reference.version)

    def _project(self, connection: sqlite3.Connection, project_id: str) -> Project:
        return ProjectRepository(connection).get(project_id)

    @staticmethod
    def _input_hash(
        operation: str,
        raw: bytes,
        prompt: ResolvedPrompt,
        *,
        upstream_document_id: str | None = None,
        authorization_id: str | None = None,
    ) -> str:
        return canonical_hash(
            {
                "authorization_id": authorization_id,
                "operation": operation,
                "prompt": {
                    "id": prompt.id,
                    "sha256": prompt.sha256,
                    "version": prompt.version,
                },
                "raw_sha256": sha256_bytes(raw),
                "upstream_document_id": upstream_document_id,
            }
        )

    @staticmethod
    def _directory(kind: DocumentKind) -> str:
        return {
            DocumentKind.QUESTIONNAIRE: "academic/questionnaire",
            DocumentKind.CONSOLIDATED_ANSWERS: "academic/consolidated-answers",
            DocumentKind.ACADEMIC_PLAN: "academic/plan",
        }[kind]

    @classmethod
    def _raw_path(cls, kind: DocumentKind, version: int) -> str:
        extension = "md" if kind is DocumentKind.ACADEMIC_PLAN else "txt"
        return f"{cls._directory(kind)}/raw/v{version:04d}.{extension}"

    @classmethod
    def _accepted_path(cls, kind: DocumentKind, raw_version: int) -> str:
        extension = "md" if kind is DocumentKind.ACADEMIC_PLAN else "json"
        return f"{cls._directory(kind)}/accepted/from-raw-v{raw_version:04d}.{extension}"

    @staticmethod
    def _prompt_path(prompt: ResolvedPrompt) -> str:
        return f"prompts/frozen/{prompt.id}/v{prompt.version:04d}.txt"

    def _freeze_prompt(self, project: Project, prompt: ResolvedPrompt) -> StoredFile:
        return self.store.write_bytes(
            project.artifact_root, self._prompt_path(prompt), prompt.content
        )

    def _ensure_prompt_artifact(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        prompt: ResolvedPrompt,
        stored: StoredFile,
    ) -> Artifact:
        repository = ArtifactRepository(connection)
        existing = repository.get_by_path(project.id, stored.relative_path)
        if existing is not None:
            if existing.sha256 != stored.sha256 or existing.byte_size != stored.byte_size:
                raise ConflictError("PROMPT_ARTIFACT_CONFLICT", "Frozen prompt metadata differs")
            return existing
        return self._register_artifact(
            connection, project, run, "prompt_snapshot", stored, prompt.version
        )

    @staticmethod
    def _register_artifact(
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        artifact_type: str,
        stored: StoredFile,
        version: int,
    ) -> Artifact:
        return ArtifactRepository(connection).add_idempotent(
            Artifact(
                id=new_id(),
                project_id=project.id,
                stage_run_id=run.id,
                artifact_type=artifact_type,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                version=version,
                created_at=utc_now(),
            )
        )

    @staticmethod
    def _next_accepted_version(
        connection: sqlite3.Connection, project_id: str, kind: DocumentKind
    ) -> int:
        row = connection.execute(
            "SELECT max(accepted_version) FROM academic_documents "
            "WHERE project_id = ? AND document_kind = ?",
            (project_id, kind.value),
        ).fetchone()
        assert row is not None
        return 1 if row[0] is None else int(row[0]) + 1

    def _artifact_content(
        self, connection: sqlite3.Connection, project: Project, artifact_id: str | None
    ) -> bytes:
        if artifact_id is None:
            raise IntegrityError(
                "ACADEMIC_ARTIFACT_REFERENCE_MISSING", "Academic artifact is not registered"
            )
        artifact = AcademicArtifactRepository(connection).get(artifact_id)
        inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
        if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
            raise IntegrityError(
                "ACADEMIC_ARTIFACT_HASH_MISMATCH",
                f"Academic artifact {artifact.relative_path!r} differs from its record",
            )
        return self.store.resolve(project.artifact_root, artifact.relative_path).read_bytes()

    def _load_answers(
        self, connection: sqlite3.Connection, document: AcademicDocument
    ) -> ConsolidatedAnswers:
        project = ProjectRepository(connection).get(document.project_id)
        raw = self._artifact_content(connection, project, document.raw_artifact_id)
        return parse_consolidated_answers(decode_academic_text(raw))

    def _current_view(self, project_id: str, kind: DocumentKind) -> AcceptedAcademicArtifact | None:
        with self.database.connection() as connection:
            document = AcademicDocumentRepository(connection).current_accepted(project_id, kind)
            return None if document is None else self._view(connection, document)

    def _view(
        self, connection: sqlite3.Connection, document: AcademicDocument
    ) -> AcceptedAcademicArtifact:
        project = ProjectRepository(connection).get(document.project_id)
        if document.accepted_artifact_id is None:
            raise IntegrityError(
                "ACADEMIC_ARTIFACT_REFERENCE_MISSING", "Accepted artifact is not registered"
            )
        artifact = AcademicArtifactRepository(connection).get(document.accepted_artifact_id)
        content = self._artifact_content(connection, project, artifact.id)
        return AcceptedAcademicArtifact(document, artifact, content)

    @staticmethod
    def _document_status(document: AcademicDocument | None) -> dict[str, object]:
        if document is None:
            return {"status": "missing"}
        return {
            "status": document.acceptance_status.value,
            "raw_version": document.raw_version,
            "accepted_version": document.accepted_version,
            "prompt": {
                "id": document.prompt_id,
                "version": document.prompt_version,
                "sha256": document.prompt_sha256,
            },
            "upstream_document_id": document.upstream_document_id,
        }
