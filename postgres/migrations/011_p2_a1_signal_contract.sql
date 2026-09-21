-- P2-A1 signal contract amendment.  This is shadow-only: no authority or
-- execution behavior is enabled by this migration.
CREATE TABLE IF NOT EXISTS strategy.entry_signals (
    signal_id text PRIMARY KEY,
    candidate_id text NOT NULL,
    evaluation_id text NOT NULL REFERENCES strategy.evaluations(evaluation_id),
    strategy_ref text NOT NULL,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    parameter_set_ref text,
    parameter_set_status text NOT NULL DEFAULT 'LEGACY_IMPLICIT_IN_STRATEGY_ID',
    strategy_instance_id text,
    instrument text NOT NULL,
    direction text,
    decision_time timestamptz NOT NULL,
    signal_emitted_at timestamptz,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    entry_type text,
    entry_price numeric,
    entry_mechanism text,
    stop_price numeric,
    risk_distance numeric,
    target_price numeric,
    target_distance numeric,
    target_r numeric,
    economic_position_id text,
    entry_opportunity_id text,
    setup_id text,
    source_event_id text,
    source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    runtime_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    evaluation_hash text NOT NULL,
    trace_hash text NOT NULL,
    terminal_state text NOT NULL,
    strategy_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    entry_signal_hash text NOT NULL,
    UNIQUE (entry_signal_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS entry_signals_canonical_result_uq
ON strategy.entry_signals (strategy_id, strategy_version, COALESCE(parameter_set_ref, ''), instrument, decision_time, candidate_id);
CREATE INDEX IF NOT EXISTS entry_signals_source_event_idx ON strategy.entry_signals(source_event_id);

CREATE TABLE IF NOT EXISTS platform.signal_ingest_quarantine (
    quarantine_id bigserial PRIMARY KEY,
    source_id text NOT NULL,
    source_offset bigint NOT NULL,
    raw_sha256 text NOT NULL,
    raw_payload text NOT NULL,
    error text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_offset, raw_sha256)
);

ALTER TABLE platform.outbox_events ADD COLUMN IF NOT EXISTS lease_owner text;
ALTER TABLE platform.outbox_events ADD COLUMN IF NOT EXISTS leased_until timestamptz;
CREATE INDEX IF NOT EXISTS outbox_lease_idx ON platform.outbox_events(publish_status, leased_until, created_at);
ALTER TABLE platform.reconciliation_findings DROP CONSTRAINT IF EXISTS reconciliation_findings_status_check;
ALTER TABLE platform.reconciliation_findings ADD CONSTRAINT reconciliation_findings_status_check CHECK (status IN ('MATCH','MISSING_LEGACY','MISSING_DATABASE','HASH_MISMATCH','VERSION_MISMATCH','TERMINAL_STATE_MISMATCH','GENERATION_MISMATCH','UNRESOLVED','EXPECTED_LAG','KNOWN_LEGACY_ANOMALY','MALFORMED_LEGACY_LINE'));
