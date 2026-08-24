CREATE TABLE browser_interaction_resolutions (
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
        REFERENCES browser_interactions(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    UNIQUE (id, project_id)
);

CREATE INDEX browser_interaction_resolutions_project_idx
ON browser_interaction_resolutions(project_id, created_at);
