CREATE UNIQUE INDEX ux_visual_figures_anchor_owner
ON visual_figures(id, project_id, visual_plan_id);

CREATE TABLE visual_anchors (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    visual_plan_id TEXT NOT NULL,
    figure_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version >= 1),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    supersedes_anchor_id TEXT,
    disposition TEXT NOT NULL CHECK (disposition IN ('processing', 'valid', 'invalid')),
    prompt_id TEXT NOT NULL,
    prompt_version INTEGER NOT NULL CHECK (prompt_version >= 1),
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256) = 64),
    raw_artifact_id TEXT,
    report_sha256 TEXT CHECK (report_sha256 IS NULL OR length(report_sha256) = 64),
    report_artifact_id TEXT,
    unit_id TEXT,
    start_offset INTEGER,
    end_offset INTEGER,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    FOREIGN KEY (figure_id, project_id, visual_plan_id)
        REFERENCES visual_figures(id, project_id, visual_plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_anchor_id, project_id, figure_id)
        REFERENCES visual_anchors(id, project_id, figure_id) ON DELETE RESTRICT,
    FOREIGN KEY (raw_artifact_id, project_id, raw_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (report_artifact_id, project_id, report_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (figure_id, unit_id)
        REFERENCES visual_figure_page_units(figure_id, unit_id) ON DELETE RESTRICT,
    UNIQUE (id, project_id, figure_id),
    UNIQUE (id, project_id, visual_plan_id, figure_id),
    UNIQUE (figure_id, version),
    UNIQUE (figure_id, input_hash),
    CHECK (
        (disposition = 'processing' AND raw_artifact_id IS NULL
            AND report_artifact_id IS NULL AND report_sha256 IS NULL AND validated_at IS NULL)
        OR (disposition IN ('valid', 'invalid') AND raw_artifact_id IS NOT NULL
            AND report_artifact_id IS NOT NULL AND report_sha256 IS NOT NULL
            AND validated_at IS NOT NULL)
    ),
    CHECK (
        (disposition = 'valid' AND unit_id IS NOT NULL AND start_offset IS NOT NULL
            AND end_offset IS NOT NULL AND start_offset >= 0 AND end_offset > start_offset)
        OR (disposition != 'valid' AND unit_id IS NULL
            AND start_offset IS NULL AND end_offset IS NULL)
    )
);

CREATE TABLE visual_finalizations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    visual_plan_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    accepted_version INTEGER NOT NULL CHECK (accepted_version >= 1),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    manifest_artifact_id TEXT,
    manifest_sha256 TEXT CHECK (manifest_sha256 IS NULL OR length(manifest_sha256) = 64),
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    FOREIGN KEY (visual_plan_id, project_id)
        REFERENCES visual_plans(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (manifest_artifact_id, project_id, manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id, visual_plan_id),
    UNIQUE (project_id, accepted_version),
    UNIQUE (project_id, input_hash),
    CHECK (
        (accepted_at IS NULL AND manifest_artifact_id IS NULL AND manifest_sha256 IS NULL)
        OR (accepted_at IS NOT NULL AND manifest_artifact_id IS NOT NULL
            AND manifest_sha256 IS NOT NULL)
    )
);

CREATE TABLE visual_finalization_anchors (
    finalization_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    visual_plan_id TEXT NOT NULL,
    figure_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    PRIMARY KEY (finalization_id, figure_id),
    FOREIGN KEY (finalization_id, project_id, visual_plan_id)
        REFERENCES visual_finalizations(id, project_id, visual_plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (anchor_id, project_id, visual_plan_id, figure_id)
        REFERENCES visual_anchors(id, project_id, visual_plan_id, figure_id) ON DELETE RESTRICT
);
