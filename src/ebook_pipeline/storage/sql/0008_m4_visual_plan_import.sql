CREATE UNIQUE INDEX ux_visual_pagination_pages_figure_binding
ON visual_pagination_pages(
    pagination_snapshot_id,
    page_key,
    chapter_id,
    document_page_number,
    eligible_page_number
);

CREATE TABLE visual_plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    raw_version INTEGER NOT NULL CHECK (raw_version >= 1),
    disposition TEXT NOT NULL CHECK (disposition IN ('processing', 'valid', 'invalid')),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    pagination_snapshot_id TEXT NOT NULL,
    pagination_manifest_artifact_id TEXT NOT NULL,
    pagination_manifest_sha256 TEXT NOT NULL CHECK (
        length(pagination_manifest_sha256) = 64
        AND pagination_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    consolidation_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    production_set_hash TEXT NOT NULL CHECK (
        length(production_set_hash) = 64 AND production_set_hash NOT GLOB '*[^0-9a-f]*'
    ),
    text_artifact_id TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (
        length(text_sha256) = 64 AND text_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    consolidation_manifest_artifact_id TEXT NOT NULL,
    consolidation_manifest_sha256 TEXT NOT NULL CHECK (
        length(consolidation_manifest_sha256) = 64
        AND consolidation_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    prompt_id TEXT NOT NULL CHECK (length(prompt_id) > 0),
    prompt_version INTEGER NOT NULL CHECK (prompt_version >= 1),
    prompt_sha256 TEXT NOT NULL CHECK (
        length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_sha256 TEXT NOT NULL CHECK (
        length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_artifact_id TEXT,
    validation_report_sha256 TEXT CHECK (
        validation_report_sha256 IS NULL OR (
            length(validation_report_sha256) = 64
            AND validation_report_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    validation_report_artifact_id TEXT,
    expected_figure_count INTEGER NOT NULL CHECK (expected_figure_count >= 0),
    figure_count INTEGER NOT NULL DEFAULT 0 CHECK (figure_count >= 0),
    created_at TEXT NOT NULL,
    validated_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (pagination_snapshot_id, project_id)
        REFERENCES visual_pagination_snapshots(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (pagination_manifest_artifact_id, project_id, pagination_manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (consolidation_id, project_id)
        REFERENCES text_consolidations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (text_artifact_id, project_id, text_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (
        consolidation_manifest_artifact_id, project_id, consolidation_manifest_sha256
    ) REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (raw_artifact_id, project_id, raw_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (validation_report_artifact_id, project_id, validation_report_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (id, project_id, pagination_snapshot_id),
    UNIQUE (project_id, raw_version),
    UNIQUE (project_id, input_hash),
    CHECK (
        (disposition = 'processing'
            AND raw_artifact_id IS NULL
            AND validation_report_artifact_id IS NULL
            AND validation_report_sha256 IS NULL
            AND figure_count = 0
            AND validated_at IS NULL)
        OR
        (disposition IN ('valid', 'invalid')
            AND raw_artifact_id IS NOT NULL
            AND validation_report_artifact_id IS NOT NULL
            AND validation_report_sha256 IS NOT NULL
            AND validated_at IS NOT NULL
            AND (disposition = 'invalid' OR figure_count = expected_figure_count))
    )
);

CREATE TABLE visual_figures (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    visual_plan_id TEXT NOT NULL,
    pagination_snapshot_id TEXT NOT NULL,
    figure_order INTEGER NOT NULL CHECK (figure_order >= 1),
    number INTEGER NOT NULL CHECK (number >= 1),
    name TEXT NOT NULL CHECK (length(name) > 0),
    editorial_page TEXT NOT NULL CHECK (length(editorial_page) > 0),
    section TEXT NOT NULL CHECK (length(section) > 0),
    exact_position TEXT NOT NULL CHECK (length(exact_position) > 0),
    main_concept TEXT NOT NULL CHECK (length(main_concept) > 0),
    conceptual_synthesis TEXT NOT NULL CHECK (length(conceptual_synthesis) > 0),
    justification TEXT NOT NULL CHECK (length(justification) > 0),
    objective TEXT NOT NULL CHECK (length(objective) > 0),
    visual_type TEXT NOT NULL CHECK (length(visual_type) > 0),
    complexity TEXT NOT NULL CHECK (
        complexity IN ('Editorial direta', 'Editorial estruturada', 'Síntese conceitual')
    ),
    generation_prompt TEXT NOT NULL CHECK (length(generation_prompt) > 0),
    document_page_number INTEGER NOT NULL CHECK (document_page_number >= 1),
    eligible_page_number INTEGER NOT NULL CHECK (eligible_page_number >= 1),
    page_key TEXT NOT NULL CHECK (page_key GLOB 'CH0[1-8]-P[0-9][0-9][0-9]'),
    chapter_id TEXT NOT NULL CHECK (chapter_id GLOB 'CH0[1-8]'),
    editorial_sha256 TEXT NOT NULL CHECK (
        length(editorial_sha256) = 64 AND editorial_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    FOREIGN KEY (visual_plan_id, project_id, pagination_snapshot_id)
        REFERENCES visual_plans(id, project_id, pagination_snapshot_id) ON DELETE RESTRICT,
    FOREIGN KEY (
        pagination_snapshot_id,
        page_key,
        chapter_id,
        document_page_number,
        eligible_page_number
    ) REFERENCES visual_pagination_pages(
        pagination_snapshot_id,
        page_key,
        chapter_id,
        document_page_number,
        eligible_page_number
    ) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (
        id,
        project_id,
        visual_plan_id,
        pagination_snapshot_id,
        document_page_number,
        page_key
    ),
    UNIQUE (visual_plan_id, figure_order),
    UNIQUE (visual_plan_id, number),
    UNIQUE (visual_plan_id, page_key)
);

CREATE TABLE visual_figure_page_units (
    figure_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    visual_plan_id TEXT NOT NULL,
    pagination_snapshot_id TEXT NOT NULL,
    document_page_number INTEGER NOT NULL,
    page_key TEXT NOT NULL,
    span_order INTEGER NOT NULL CHECK (span_order >= 1),
    unit_id TEXT NOT NULL CHECK (length(unit_id) > 0),
    PRIMARY KEY (figure_id, span_order),
    FOREIGN KEY (
        figure_id,
        project_id,
        visual_plan_id,
        pagination_snapshot_id,
        document_page_number,
        page_key
    ) REFERENCES visual_figures(
        id,
        project_id,
        visual_plan_id,
        pagination_snapshot_id,
        document_page_number,
        page_key
    ) ON DELETE RESTRICT,
    FOREIGN KEY (pagination_snapshot_id, document_page_number, unit_id)
        REFERENCES visual_pagination_page_unit_spans(
            pagination_snapshot_id,
            document_page_number,
            unit_id
        ) ON DELETE RESTRICT,
    UNIQUE (figure_id, unit_id)
);
