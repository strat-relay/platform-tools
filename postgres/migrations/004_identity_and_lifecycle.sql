CREATE TABLE IF NOT EXISTS platform.strategy_versions (
    strategy_version_id text PRIMARY KEY,
    strategy_version text NOT NULL,
    schema_version text,
    source_hash text,
    created_at timestamptz,
    UNIQUE(strategy_version, schema_version, source_hash)
);
CREATE TABLE IF NOT EXISTS platform.configuration_versions (
    configuration_version_id text PRIMARY KEY,
    configuration_hash text NOT NULL UNIQUE,
    configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform.freeze_manifests (
    freeze_manifest_id text PRIMARY KEY,
    strategy_version_id text REFERENCES platform.strategy_versions(strategy_version_id),
    configuration_version_id text REFERENCES platform.configuration_versions(configuration_version_id),
    freeze_timestamp timestamptz,
    manifest_hash text NOT NULL UNIQUE,
    manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.runner_state (
    runner_id text PRIMARY KEY,
    strategy_version_id text REFERENCES platform.strategy_versions(strategy_version_id),
    configuration_version_id text REFERENCES platform.configuration_versions(configuration_version_id),
    freeze_manifest_id text REFERENCES platform.freeze_manifests(freeze_manifest_id),
    status text, started_at timestamptz, last_processed_at timestamptz,
    state_version text NOT NULL, prospective_boundary jsonb,
    kill_switch boolean, poll_interval_seconds numeric,
    source_state_sha256 text, import_batch_id text, updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE strategy.phase6_symbol_progress ADD COLUMN IF NOT EXISTS source_timestamp text;
ALTER TABLE strategy.phase6_symbol_progress ADD COLUMN IF NOT EXISTS import_batch_id text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS strategy_version_id text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS configuration_version_id text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS freeze_manifest_id text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS import_batch_id text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS reentry_state text;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS reentry_eligible boolean;
ALTER TABLE strategy.phase6_setups ADD COLUMN IF NOT EXISTS entry_attempt_count integer;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS strategy_version_id text;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS configuration_version_id text;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS freeze_manifest_id text;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS import_batch_id text;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS attempt_number integer;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS entry numeric;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS original_stop numeric;
ALTER TABLE strategy.phase6_entry_opportunities ADD COLUMN IF NOT EXISTS target_consumed boolean;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS strategy_version_id text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS configuration_version_id text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS freeze_manifest_id text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS import_batch_id text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS symbol text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS direction text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS entry numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS original_stop numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS current_stop numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS size numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS opened_at text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS closed_at text;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS realized_pnl numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS mfe_r numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS mae_r numeric;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS target_consumed boolean;
ALTER TABLE strategy.phase6_economic_positions ADD COLUMN IF NOT EXISTS reentry_state text;
CREATE TABLE IF NOT EXISTS strategy.phase6_lifecycle_events (
    event_id text PRIMARY KEY, setup_id text, entry_opportunity_id text,
    economic_position_id text, event_time text, event_type text NOT NULL,
    source text, payload jsonb NOT NULL, import_batch_id text,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS phase6_lifecycle_setup_idx ON strategy.phase6_lifecycle_events(setup_id);
CREATE INDEX IF NOT EXISTS phase6_lifecycle_position_idx ON strategy.phase6_lifecycle_events(economic_position_id);
CREATE TABLE IF NOT EXISTS telemetry.phase6_unattached_events (
    event_id text PRIMARY KEY, event_type text NOT NULL, event_time text,
    source text, source_file text, source_line bigint, payload jsonb NOT NULL,
    linkage_reason text NOT NULL, import_batch_id text,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform.import_batches (
    batch_id text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
    import_boundary text NOT NULL, source_snapshot_times jsonb NOT NULL,
    source_files jsonb NOT NULL, source_hashes jsonb NOT NULL,
    strategy_version_id text, configuration_version_id text, freeze_manifest_id text,
    counts_attempted jsonb NOT NULL DEFAULT '{}'::jsonb, counts_inserted jsonb NOT NULL DEFAULT '{}'::jsonb,
    counts_deduplicated jsonb NOT NULL DEFAULT '{}'::jsonb, counts_rejected jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation_result jsonb NOT NULL DEFAULT '{}'::jsonb, importer_version text NOT NULL
);
