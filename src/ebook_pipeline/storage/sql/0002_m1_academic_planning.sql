CREATE UNIQUE INDEX artifacts_id_project_uq ON artifacts(id, project_id);

CREATE TABLE academic_plan_authorizations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    answers_document_id TEXT NOT NULL UNIQUE,
    conflict_count INTEGER NOT NULL CHECK (conflict_count = 0),
    authorized_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    FOREIGN KEY (answers_document_id, project_id)
        REFERENCES academic_documents(id, project_id) ON DELETE RESTRICT
);

CREATE TABLE academic_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN ('questionnaire', 'consolidated_answers', 'academic_plan')
    ),
    acceptance_status TEXT NOT NULL CHECK (
        acceptance_status IN ('processing', 'accepted', 'review_required', 'rejected')
    ),
    raw_version INTEGER NOT NULL CHECK (raw_version >= 1),
    accepted_version INTEGER CHECK (accepted_version >= 1),
    raw_sha256 TEXT NOT NULL CHECK (
        length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_artifact_id TEXT,
    accepted_artifact_id TEXT,
    prompt_id TEXT NOT NULL CHECK (length(prompt_id) > 0),
    prompt_version INTEGER NOT NULL CHECK (prompt_version >= 1),
    prompt_sha256 TEXT NOT NULL CHECK (
        length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    upstream_document_id TEXT,
    authorization_id TEXT,
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (raw_artifact_id, project_id)
        REFERENCES artifacts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (accepted_artifact_id, project_id)
        REFERENCES artifacts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (upstream_document_id, project_id)
        REFERENCES academic_documents(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (authorization_id, project_id)
        REFERENCES academic_plan_authorizations(id, project_id) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, document_kind, raw_version),
    CHECK (
        (acceptance_status = 'accepted'
            AND accepted_version IS NOT NULL
            AND accepted_artifact_id IS NOT NULL
            AND accepted_at IS NOT NULL)
        OR
        (acceptance_status != 'accepted'
            AND accepted_version IS NULL
            AND accepted_artifact_id IS NULL
            AND accepted_at IS NULL)
    ),
    CHECK (
        (document_kind = 'questionnaire'
            AND upstream_document_id IS NULL AND authorization_id IS NULL)
        OR
        (document_kind = 'consolidated_answers'
            AND upstream_document_id IS NOT NULL AND authorization_id IS NULL)
        OR
        (document_kind = 'academic_plan'
            AND upstream_document_id IS NOT NULL AND authorization_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX academic_documents_accepted_version_uq
ON academic_documents(project_id, document_kind, accepted_version)
WHERE accepted_version IS NOT NULL;

CREATE INDEX academic_documents_current_idx
ON academic_documents(project_id, document_kind, acceptance_status, accepted_version);

CREATE TABLE academic_review_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    questionnaire_document_id TEXT NOT NULL UNIQUE,
    stage_run_id TEXT NOT NULL UNIQUE,
    decision TEXT NOT NULL CHECK (decision = 'confirm_observed_questionnaire'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (questionnaire_document_id, project_id)
        REFERENCES academic_documents(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT
);
