CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    summary_json TEXT,
    config_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    commission_name TEXT,
    student_dni TEXT,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint_data TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS sheet_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    hash TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    commission TEXT,
    dni TEXT,
    discrepancy_type TEXT NOT NULL,
    field TEXT,
    expected_value TEXT,
    actual_value TEXT,
    confidence REAL,
    resolution TEXT,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    discrepancy_id INTEGER,
    input_hash TEXT NOT NULL,
    model_used TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    raw_response TEXT,
    decision_json TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id)
);

CREATE TABLE IF NOT EXISTS patch_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    row_number INTEGER,
    column TEXT,
    old_value TEXT,
    new_value TEXT,
    status TEXT NOT NULL,
    applied_at TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS pending_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    discrepancy_id INTEGER,
    reason TEXT NOT NULL,
    context_json TEXT,
    status TEXT NOT NULL,
    reviewed_at TEXT,
    reviewer_notes TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id)
);

CREATE TABLE IF NOT EXISTS review_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL UNIQUE,
    run_id TEXT,
    commission TEXT NOT NULL,
    dni TEXT NOT NULL,
    problem TEXT NOT NULL,
    resolution TEXT NOT NULL,
    monto REAL,
    concepto_tipo TEXT,
    pricing_inscripcion REAL,
    pricing_cuota REAL,
    monto_ratio REAL,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollback_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    row_number INTEGER,
    row_snapshot TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
