PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    roots_json TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS people (
    person_id TEXT PRIMARY KEY,
    alias TEXT UNIQUE,
    canonical_email TEXT,
    display_name TEXT,
    company_type TEXT NOT NULL DEFAULT 'unknown',
    source_origin TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS person_snapshots (
    id INTEGER PRIMARY KEY,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_ref TEXT,
    full_name TEXT,
    title TEXT,
    organization_name TEXT,
    department_name TEXT,
    department_id TEXT,
    worker_type TEXT,
    employee_id TEXT,
    manager_person_id TEXT,
    manager_alias TEXT,
    manager_name TEXT,
    location_text TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    timezone_text TEXT,
    email TEXT,
    assistant_person_id TEXT,
    assistant_alias TEXT,
    assistant_name TEXT,
    assistant_title TEXT,
    direct_report_count INTEGER,
    employee_direct_count INTEGER,
    contingent_direct_count INTEGER,
    phones_json TEXT,
    pto_json TEXT,
    webex_status_json TEXT,
    badges_json TEXT,
    label_text TEXT,
    source_priority INTEGER NOT NULL DEFAULT 100,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_person_snapshots_person_captured
    ON person_snapshots(person_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_person_snapshots_run_person
    ON person_snapshots(crawl_run_id, person_id);

CREATE TABLE IF NOT EXISTS person_phones (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES person_snapshots(id) ON DELETE CASCADE,
    phone_label TEXT,
    phone_kind TEXT,
    phone_value TEXT,
    normalized_value TEXT,
    is_internal INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_person_phones_snapshot
    ON person_phones(snapshot_id);

CREATE TABLE IF NOT EXISTS person_paths (
    id INTEGER PRIMARY KEY,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    root_person_id TEXT NOT NULL,
    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    manager_person_id TEXT,
    depth INTEGER NOT NULL,
    branch_label TEXT,
    path_scope TEXT NOT NULL,
    UNIQUE (crawl_run_id, root_person_id, person_id, path_scope)
);

CREATE INDEX IF NOT EXISTS idx_person_paths_run_root_depth
    ON person_paths(crawl_run_id, root_person_id, depth);

CREATE INDEX IF NOT EXISTS idx_person_paths_run_root_scope_manager
    ON person_paths(crawl_run_id, root_person_id, path_scope, manager_person_id);

CREATE INDEX IF NOT EXISTS idx_person_paths_person_context
    ON person_paths(person_id, crawl_run_id, root_person_id, path_scope);

CREATE TABLE IF NOT EXISTS person_labels (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES person_snapshots(id) ON DELETE CASCADE,
    label_key TEXT NOT NULL,
    label_value TEXT NOT NULL,
    label_source TEXT NOT NULL,
    UNIQUE (snapshot_id, label_key, label_value)
);

CREATE INDEX IF NOT EXISTS idx_person_labels_snapshot
    ON person_labels(snapshot_id);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    discovered_at TEXT NOT NULL,
    source_system TEXT NOT NULL,
    query_text TEXT,
    external_ref TEXT UNIQUE,
    title TEXT,
    url TEXT,
    owner_name TEXT,
    owner_alias TEXT,
    company_type TEXT NOT NULL DEFAULT 'unknown',
    archived_path TEXT,
    extracted_text_path TEXT,
    mime_type TEXT,
    file_extension TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    raw_metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_discovered_at
    ON documents(discovered_at DESC);

CREATE TABLE IF NOT EXISTS document_mentions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    mention_type TEXT NOT NULL,
    mention_text TEXT,
    confidence REAL,
    UNIQUE (document_id, person_id, mention_type, mention_text)
);

CREATE INDEX IF NOT EXISTS idx_document_mentions_doc
    ON document_mentions(document_id);

CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    system_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS person_fts USING fts5(
    person_id,
    alias,
    full_name,
    title,
    organization_name,
    location_text,
    label_text,
    content=''
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    title,
    owner_name,
    owner_alias,
    url,
    query_text,
    extracted_text,
    content=''
);
