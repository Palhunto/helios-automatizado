CREATE UNIQUE INDEX artifacts_id_project_sha_uq
ON artifacts(id, project_id, sha256);

CREATE TABLE writing_contexts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version >= 1),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    answers_document_id TEXT NOT NULL,
    answers_artifact_id TEXT NOT NULL,
    answers_sha256 TEXT NOT NULL CHECK (
        length(answers_sha256) = 64 AND answers_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    plan_document_id TEXT NOT NULL,
    plan_artifact_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL CHECK (
        length(plan_sha256) = 64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    writing_prompt_id TEXT NOT NULL CHECK (length(writing_prompt_id) > 0),
    writing_prompt_version INTEGER NOT NULL CHECK (writing_prompt_version >= 1),
    writing_prompt_sha256 TEXT NOT NULL CHECK (
        length(writing_prompt_sha256) = 64 AND writing_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    contract_id TEXT NOT NULL CHECK (length(contract_id) > 0),
    contract_version INTEGER NOT NULL CHECK (contract_version >= 1),
    contract_sha256 TEXT NOT NULL CHECK (
        length(contract_sha256) = 64 AND contract_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    request_prompt_id TEXT NOT NULL CHECK (length(request_prompt_id) > 0),
    request_prompt_version INTEGER NOT NULL CHECK (request_prompt_version >= 1),
    request_prompt_sha256 TEXT NOT NULL CHECK (
        length(request_prompt_sha256) = 64 AND request_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    package_sha256 TEXT NOT NULL CHECK (
        length(package_sha256) = 64 AND package_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_sha256 TEXT NOT NULL CHECK (
        length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    package_artifact_id TEXT,
    manifest_artifact_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (answers_document_id, project_id)
        REFERENCES academic_documents(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (answers_artifact_id, project_id, answers_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (plan_document_id, project_id)
        REFERENCES academic_documents(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (plan_artifact_id, project_id, plan_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (package_artifact_id, project_id, package_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (manifest_artifact_id, project_id, manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, version),
    UNIQUE (project_id, input_hash)
);

CREATE TABLE writing_acknowledgements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    raw_version INTEGER NOT NULL CHECK (raw_version >= 1),
    raw_sha256 TEXT NOT NULL CHECK (
        length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_artifact_id TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (raw_artifact_id, project_id, raw_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (context_id, raw_version),
    UNIQUE (context_id, raw_sha256)
);

CREATE TABLE writing_unit_preparations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    unit_id TEXT NOT NULL CHECK (length(unit_id) > 0),
    version INTEGER NOT NULL CHECK (version >= 1),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_sha256 TEXT NOT NULL CHECK (
        length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    request_artifact_id TEXT,
    manifest_artifact_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_artifact_id, project_id, request_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (manifest_artifact_id, project_id, manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (id, project_id, context_id, unit_id),
    UNIQUE (context_id, unit_id, version)
);

CREATE TABLE text_unit_submissions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    preparation_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    unit_id TEXT NOT NULL CHECK (length(unit_id) > 0),
    raw_version INTEGER NOT NULL CHECK (raw_version >= 1),
    accepted_version INTEGER CHECK (accepted_version >= 1),
    disposition TEXT NOT NULL CHECK (
        disposition IN ('processing', 'accepted', 'review_required', 'rejected')
    ),
    raw_sha256 TEXT NOT NULL CHECK (
        length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_artifact_id TEXT,
    validation_report_sha256 TEXT NOT NULL CHECK (
        length(validation_report_sha256) = 64
        AND validation_report_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    validation_report_artifact_id TEXT,
    accepted_artifact_id TEXT,
    citation_ledger_sha256 TEXT CHECK (
        citation_ledger_sha256 IS NULL OR (
            length(citation_ledger_sha256) = 64
            AND citation_ledger_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    citation_ledger_artifact_id TEXT,
    character_count INTEGER NOT NULL CHECK (character_count >= 0),
    warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (preparation_id, project_id, context_id, unit_id)
        REFERENCES writing_unit_preparations(id, project_id, context_id, unit_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (raw_artifact_id, project_id, raw_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (validation_report_artifact_id, project_id, validation_report_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (accepted_artifact_id, project_id, raw_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (citation_ledger_artifact_id, project_id, citation_ledger_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (id, project_id, context_id, unit_id, accepted_version, accepted_artifact_id),
    UNIQUE (context_id, unit_id, raw_version),
    UNIQUE (preparation_id, raw_sha256),
    CHECK (
        (disposition = 'accepted' AND accepted_version IS NOT NULL
            AND accepted_artifact_id IS NOT NULL AND accepted_at IS NOT NULL
            AND citation_ledger_sha256 IS NOT NULL
            AND citation_ledger_artifact_id IS NOT NULL)
        OR
        (disposition != 'accepted' AND accepted_version IS NULL
            AND accepted_artifact_id IS NULL AND accepted_at IS NULL
            AND citation_ledger_sha256 IS NULL
            AND citation_ledger_artifact_id IS NULL)
    )
);

CREATE UNIQUE INDEX text_unit_submissions_accepted_version_uq
ON text_unit_submissions(context_id, unit_id, accepted_version)
WHERE accepted_version IS NOT NULL;

CREATE INDEX text_unit_submissions_selection_idx
ON text_unit_submissions(context_id, unit_id, disposition, accepted_version);

CREATE TABLE writing_preparation_dependencies (
    preparation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    dependency_unit_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    accepted_version INTEGER NOT NULL CHECK (accepted_version >= 1),
    artifact_id TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (preparation_id, dependency_unit_id),
    FOREIGN KEY (preparation_id, project_id)
        REFERENCES writing_unit_preparations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (
        submission_id, project_id, context_id, dependency_unit_id,
        accepted_version, artifact_id
    ) REFERENCES text_unit_submissions(
        id, project_id, context_id, unit_id, accepted_version, accepted_artifact_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id, project_id, sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT
);

CREATE TABLE citation_occurrences (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    raw_citation_text TEXT NOT NULL CHECK (length(raw_citation_text) > 0),
    observed_author TEXT NOT NULL CHECK (length(observed_author) > 0),
    normalized_author TEXT NOT NULL CHECK (length(normalized_author) > 0),
    year_text TEXT NOT NULL CHECK (length(year_text) > 0),
    parsed_year INTEGER CHECK (parsed_year BETWEEN 1000 AND 2999),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    extraction_rule_version INTEGER NOT NULL CHECK (extraction_rule_version >= 1),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (submission_id, project_id)
        REFERENCES text_unit_submissions(id, project_id) ON DELETE RESTRICT,
    UNIQUE (submission_id, ordinal),
    UNIQUE (submission_id, start_offset, end_offset)
);

CREATE TABLE text_consolidations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version >= 1),
    production_set_hash TEXT NOT NULL CHECK (
        length(production_set_hash) = 64 AND production_set_hash NOT GLOB '*[^0-9a-f]*'
    ),
    separator TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (
        length(text_sha256) = 64 AND text_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_sha256 TEXT NOT NULL CHECK (
        length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    text_artifact_id TEXT,
    manifest_artifact_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (text_artifact_id, project_id, text_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (manifest_artifact_id, project_id, manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, version),
    UNIQUE (context_id, production_set_hash)
);

CREATE TABLE text_consolidation_members (
    consolidation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    unit_order INTEGER NOT NULL CHECK (unit_order >= 1),
    submission_id TEXT NOT NULL,
    accepted_version INTEGER NOT NULL CHECK (accepted_version >= 1),
    artifact_id TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (consolidation_id, unit_id),
    UNIQUE (consolidation_id, unit_order),
    FOREIGN KEY (consolidation_id, project_id)
        REFERENCES text_consolidations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (
        submission_id, project_id, context_id, unit_id,
        accepted_version, artifact_id
    ) REFERENCES text_unit_submissions(
        id, project_id, context_id, unit_id, accepted_version, accepted_artifact_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id, project_id, sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT
);
