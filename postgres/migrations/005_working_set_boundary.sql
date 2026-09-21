CREATE TABLE IF NOT EXISTS platform.migration_boundaries (
    boundary_id text PRIMARY KEY,
    mode text NOT NULL CHECK (mode = 'WORKING_SET'),
    captured_at timestamptz NOT NULL,
    event_cutoff timestamptz NOT NULL,
    source_files jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_hashes jsonb NOT NULL DEFAULT '{}'::jsonb,
    symbol_cursors jsonb NOT NULL DEFAULT '{}'::jsonb,
    archive_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
    preflight_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Canonical operational position state.  The legacy phase6_* tables remain
-- readable for audit; no second mutable position index is introduced.
CREATE TABLE IF NOT EXISTS strategy.economic_positions (
    economic_position_id text PRIMARY KEY,
    strategy_version_id text REFERENCES platform.strategy_versions(strategy_version_id),
    configuration_version_id text REFERENCES platform.configuration_versions(configuration_version_id),
    freeze_manifest_id text REFERENCES platform.freeze_manifests(freeze_manifest_id),
    import_batch_id text,
    entry_opportunity_id text NOT NULL,
    setup_id text NOT NULL,
    symbol text,
    direction text,
    status text NOT NULL,
    entry numeric,
    original_stop numeric,
    current_stop numeric,
    target numeric,
    mfe_price numeric,
    mae_price numeric,
    realized_r numeric,
    opened_at text,
    closed_at text,
    exit_reason text,
    reentry_state text,
    target_consumed boolean NOT NULL DEFAULT false,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS economic_positions_active_idx ON strategy.economic_positions(status) WHERE status = 'OPEN';
