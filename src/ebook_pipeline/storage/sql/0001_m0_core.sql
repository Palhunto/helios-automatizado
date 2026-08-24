CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    slug TEXT NOT NULL UNIQUE
        CHECK (
            length(slug) BETWEEN 1 AND 80
            AND slug = lower(slug)
            AND slug NOT GLOB '*[^a-z0-9-]*'
            AND slug NOT LIKE '-%'
            AND slug NOT LIKE '%-'
            AND slug NOT LIKE '%--%'
        ),
    config_path TEXT NOT NULL CHECK (length(config_path) > 0),
    artifact_root TEXT NOT NULL UNIQUE CHECK (length(artifact_root) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE stage_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_id TEXT NOT NULL CHECK (length(stage_id) > 0),
    unit_id TEXT NOT NULL CHECK (length(unit_id) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'done', 'failed', 'pending_retry', 'blocked', 'skipped')
    ),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        length(idempotency_key) = 64 AND idempotency_key NOT GLOB '*[^0-9a-f]*'
    ),
    version INTEGER NOT NULL CHECK (version >= 1),
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1 AND attempt <= max_attempts),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes_run_id TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, stage_id, unit_id, input_hash, version),
    CHECK (status != 'running' OR (started_at IS NOT NULL AND finished_at IS NULL)),
    CHECK (status NOT IN ('done', 'failed', 'skipped') OR finished_at IS NOT NULL)
);

CREATE INDEX stage_runs_project_status_idx ON stage_runs(project_id, status);
CREATE INDEX stage_runs_identity_idx ON stage_runs(project_id, stage_id, unit_id, version);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (length(artifact_type) > 0),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    UNIQUE (project_id, relative_path),
    UNIQUE (stage_run_id, artifact_type, version)
);

CREATE INDEX artifacts_project_idx ON artifacts(project_id);

CREATE TABLE error_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (length(code) > 0),
    message TEXT NOT NULL CHECK (length(message) > 0),
    recoverable INTEGER NOT NULL CHECK (recoverable IN (0, 1)),
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    CHECK (
        (resolved_at IS NULL AND resolution IS NULL)
        OR (resolved_at IS NOT NULL AND length(resolution) > 0)
    )
);

CREATE INDEX error_records_project_idx ON error_records(project_id, created_at);
