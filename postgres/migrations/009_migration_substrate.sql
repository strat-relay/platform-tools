-- P0/P1 migration substrate.  These tables are dormant until a domain opts in;
-- they do not change legacy authority or enable any cutover.
CREATE TABLE IF NOT EXISTS platform.migration_states (
    domain text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('NOT_STARTED','SHADOW_WRITE','RECONCILING','DB_PRIMARY',
        'EVENT_SHADOW','EVENT_PRIMARY','LEGACY_READ_DISABLED','LEGACY_WRITE_DISABLED','RETIRED')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.migration_gate_evidence (
    domain text NOT NULL,
    gate text NOT NULL,
    approved boolean NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (domain, gate)
);

CREATE TABLE IF NOT EXISTS platform.reconciliation_runs (
    run_id text PRIMARY KEY,
    domain text NOT NULL,
    status text NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS platform.reconciliation_findings (
    run_id text NOT NULL REFERENCES platform.reconciliation_runs(run_id) ON DELETE CASCADE,
    identity text NOT NULL,
    status text NOT NULL CHECK (status IN ('MATCH','MISSING_LEGACY','MISSING_DATABASE','HASH_MISMATCH',
        'VERSION_MISMATCH','TERMINAL_STATE_MISMATCH','GENERATION_MISMATCH','UNRESOLVED')),
    detail text NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, identity)
);
