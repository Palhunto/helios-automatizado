CREATE TABLE visual_pagination_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage_run_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version >= 1),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
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
    writing_contract_id TEXT NOT NULL CHECK (length(writing_contract_id) > 0),
    writing_contract_version INTEGER NOT NULL CHECK (writing_contract_version >= 1),
    writing_contract_sha256 TEXT NOT NULL CHECK (
        length(writing_contract_sha256) = 64
        AND writing_contract_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    layout_id TEXT NOT NULL CHECK (length(layout_id) > 0),
    layout_version INTEGER NOT NULL CHECK (layout_version >= 1),
    layout_sha256 TEXT NOT NULL CHECK (
        length(layout_sha256) = 64 AND layout_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    renderer_fingerprint TEXT NOT NULL CHECK (
        length(renderer_fingerprint) = 64
        AND renderer_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    html_sha256 TEXT CHECK (
        html_sha256 IS NULL OR (
            length(html_sha256) = 64 AND html_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    pdf_sha256 TEXT CHECK (
        pdf_sha256 IS NULL OR (
            length(pdf_sha256) = 64 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    manifest_sha256 TEXT CHECK (
        manifest_sha256 IS NULL OR (
            length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    html_artifact_id TEXT,
    pdf_artifact_id TEXT,
    manifest_artifact_id TEXT,
    document_page_count INTEGER NOT NULL DEFAULT 0 CHECK (document_page_count >= 0),
    eligible_page_count INTEGER NOT NULL DEFAULT 0 CHECK (eligible_page_count >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (stage_run_id, project_id)
        REFERENCES stage_runs(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (consolidation_id, project_id)
        REFERENCES text_consolidations(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (context_id, project_id)
        REFERENCES writing_contexts(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (text_artifact_id, project_id, text_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (
        consolidation_manifest_artifact_id, project_id, consolidation_manifest_sha256
    ) REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (html_artifact_id, project_id, html_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (pdf_artifact_id, project_id, pdf_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    FOREIGN KEY (manifest_artifact_id, project_id, manifest_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, version),
    UNIQUE (project_id, input_hash),
    CHECK (
        (html_artifact_id IS NULL AND html_sha256 IS NULL
            AND pdf_artifact_id IS NULL AND pdf_sha256 IS NULL
            AND manifest_artifact_id IS NULL AND manifest_sha256 IS NULL
            AND document_page_count = 0 AND eligible_page_count = 0)
        OR
        (html_artifact_id IS NOT NULL AND html_sha256 IS NOT NULL
            AND pdf_artifact_id IS NOT NULL AND pdf_sha256 IS NOT NULL
            AND manifest_artifact_id IS NOT NULL AND manifest_sha256 IS NOT NULL
            AND document_page_count >= 1)
    )
);

CREATE TABLE visual_pagination_pages (
    pagination_snapshot_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_page_number INTEGER NOT NULL CHECK (document_page_number >= 1),
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    eligible_page_number INTEGER CHECK (eligible_page_number >= 1),
    chapter_id TEXT CHECK (chapter_id GLOB 'CH0[1-8]'),
    chapter_page_number INTEGER CHECK (chapter_page_number >= 1),
    page_key TEXT,
    page_source_sha256 TEXT NOT NULL CHECK (
        length(page_source_sha256) = 64 AND page_source_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (pagination_snapshot_id, document_page_number),
    FOREIGN KEY (pagination_snapshot_id, project_id)
        REFERENCES visual_pagination_snapshots(id, project_id) ON DELETE RESTRICT,
    UNIQUE (pagination_snapshot_id, eligible_page_number),
    UNIQUE (pagination_snapshot_id, page_key),
    UNIQUE (pagination_snapshot_id, project_id, document_page_number),
    CHECK (
        (eligible = 1 AND eligible_page_number IS NOT NULL AND chapter_id IS NOT NULL
            AND chapter_page_number IS NOT NULL AND page_key IS NOT NULL
            AND page_key = chapter_id || '-P' || printf('%03d', chapter_page_number))
        OR
        (eligible = 0 AND eligible_page_number IS NULL AND chapter_id IS NULL
            AND chapter_page_number IS NULL AND page_key IS NULL)
    )
);

CREATE TABLE visual_pagination_page_unit_spans (
    pagination_snapshot_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_page_number INTEGER NOT NULL,
    span_order INTEGER NOT NULL CHECK (span_order >= 1),
    unit_id TEXT NOT NULL CHECK (length(unit_id) > 0),
    artifact_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (
        length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    global_char_start INTEGER NOT NULL CHECK (global_char_start >= 0),
    global_char_end INTEGER NOT NULL CHECK (global_char_end > global_char_start),
    global_byte_start INTEGER NOT NULL CHECK (global_byte_start >= 0),
    global_byte_end INTEGER NOT NULL CHECK (global_byte_end > global_byte_start),
    unit_char_start INTEGER NOT NULL CHECK (unit_char_start >= 0),
    unit_char_end INTEGER NOT NULL CHECK (unit_char_end > unit_char_start),
    unit_byte_start INTEGER NOT NULL CHECK (unit_byte_start >= 0),
    unit_byte_end INTEGER NOT NULL CHECK (unit_byte_end > unit_byte_start),
    PRIMARY KEY (pagination_snapshot_id, document_page_number, span_order),
    FOREIGN KEY (pagination_snapshot_id, project_id, document_page_number)
        REFERENCES visual_pagination_pages(
            pagination_snapshot_id, project_id, document_page_number
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id, project_id, source_sha256)
        REFERENCES artifacts(id, project_id, sha256) ON DELETE RESTRICT,
    UNIQUE (pagination_snapshot_id, document_page_number, unit_id)
);
