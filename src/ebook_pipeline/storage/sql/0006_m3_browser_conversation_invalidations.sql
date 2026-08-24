PRAGMA defer_foreign_keys = ON;

CREATE TABLE browser_conversations_v6 (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('chatgpt_web')),
    status TEXT NOT NULL CHECK (status IN ('provisioning', 'ready', 'blocked')),
    conversation_path TEXT CHECK (
        conversation_path IS NULL
        OR (conversation_path GLOB '/c/*' AND instr(conversation_path, '?') = 0
            AND instr(conversation_path, '#') = 0)
    ),
    first_turn_fingerprint TEXT CHECK (
        first_turn_fingerprint IS NULL OR (
            length(first_turn_fingerprint) = 64
            AND first_turn_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    ),
    provisioning_baseline_json TEXT CHECK (
        provisioning_baseline_json IS NULL OR json_valid(provisioning_baseline_json)
    ),
    provisioning_started_at TEXT NOT NULL,
    ready_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (provider, conversation_path),
    CHECK (
        (status = 'ready' AND conversation_path IS NOT NULL AND ready_at IS NOT NULL)
        OR (status != 'ready')
    )
);

CREATE TABLE browser_interactions_v6 (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('context_load', 'unit_request')),
    unit_id TEXT,
    preparation_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'prepared', 'sending', 'sent', 'streaming', 'captured', 'imported',
            'failed', 'blocked'
        )
    ),
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1 AND attempt <= max_attempts),
    request_artifact_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    transport_fingerprint TEXT NOT NULL CHECK (
        length(transport_fingerprint) = 64
        AND transport_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    response_artifact_id TEXT,
    response_sha256 TEXT CHECK (
        response_sha256 IS NULL OR (
            length(response_sha256) = 64
            AND response_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    capture_method_version TEXT,
    imported_entity_type TEXT,
    imported_entity_id TEXT,
    sent_at TEXT,
    captured_at TEXT,
    imported_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes_interaction_id TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (conversation_id, project_id)
        REFERENCES browser_conversations_v6(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (preparation_id, project_id, context_id, unit_id)
        REFERENCES writing_unit_preparations(id, project_id, context_id, unit_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (request_artifact_id, project_id, request_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (response_artifact_id, project_id, response_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_interaction_id) REFERENCES browser_interactions_v6(id)
        ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (conversation_id, request_sha256, attempt),
    CHECK (
        (kind = 'context_load' AND unit_id IS NULL AND preparation_id IS NULL)
        OR (kind = 'unit_request' AND unit_id IS NOT NULL AND preparation_id IS NOT NULL)
    ),
    CHECK (
        (status IN ('captured', 'imported') AND response_artifact_id IS NOT NULL
            AND response_sha256 IS NOT NULL AND capture_method_version IS NOT NULL
            AND captured_at IS NOT NULL)
        OR status NOT IN ('captured', 'imported')
    ),
    CHECK (
        (status = 'imported' AND imported_entity_type IS NOT NULL
            AND imported_entity_id IS NOT NULL AND imported_at IS NOT NULL)
        OR status != 'imported'
    )
);

CREATE TABLE browser_interaction_events_v6 (
    id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    previous_status TEXT,
    status TEXT NOT NULL,
    evidence_json TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (interaction_id, project_id)
        REFERENCES browser_interactions_v6(id, project_id) ON DELETE RESTRICT,
    UNIQUE (interaction_id, created_at, status)
);

CREATE TABLE browser_interaction_resolutions_v6 (
    id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK (resolution IN ('operator_abandoned')),
    operator TEXT NOT NULL CHECK (length(trim(operator)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    conversation_action TEXT NOT NULL CHECK (
        conversation_action IN ('conversation_retained', 'provisioning_attempt_closed')
    ),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (interaction_id, project_id)
        REFERENCES browser_interactions_v6(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    UNIQUE (id, project_id)
);

INSERT INTO browser_conversations_v6 SELECT * FROM browser_conversations;
INSERT INTO browser_interactions_v6 SELECT * FROM browser_interactions;
INSERT INTO browser_interaction_events_v6 SELECT * FROM browser_interaction_events;
INSERT INTO browser_interaction_resolutions_v6 SELECT * FROM browser_interaction_resolutions;

DROP TABLE browser_interaction_resolutions;
DROP TABLE browser_interaction_events;
DROP TABLE browser_interactions;
DROP TABLE browser_conversations;

ALTER TABLE browser_conversations_v6 RENAME TO browser_conversations;
ALTER TABLE browser_interactions_v6 RENAME TO browser_interactions;
ALTER TABLE browser_interaction_events_v6 RENAME TO browser_interaction_events;
ALTER TABLE browser_interaction_resolutions_v6 RENAME TO browser_interaction_resolutions;

CREATE INDEX browser_interactions_recovery_idx
ON browser_interactions(project_id, status, updated_at);

CREATE INDEX browser_interaction_resolutions_project_idx
ON browser_interaction_resolutions(project_id, created_at);

CREATE TABLE browser_conversation_invalidations (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    replacement_conversation_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK (reason IN ('legacy_invalid_conversation_path')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id, project_id)
        REFERENCES browser_conversations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (replacement_conversation_id, project_id)
        REFERENCES browser_conversations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    CHECK (conversation_id != replacement_conversation_id)
);

CREATE INDEX browser_conversation_invalidations_project_idx
ON browser_conversation_invalidations(project_id, created_at);
