-- Durable state for the platform-owned Unified Outcome Resolver.
-- This migration is additive: legacy outcome rows and strategy setup ledgers are
-- preserved while the resolver acquires authority one signal at a time.
CREATE TABLE IF NOT EXISTS platform.outcome_resolver_signal_state (
    signal_id text PRIMARY KEY REFERENCES strategy.entry_signals(signal_id),
    contract_version text NOT NULL,
    activation_state text NOT NULL CHECK (activation_state IN ('PENDING', 'ACTIVE', 'EXPIRED_UNFILLED')),
    activated_at timestamptz,
    next_candle_open timestamptz,
    last_candle_close timestamptz,
    coverage_end timestamptz,
    resolution_state text NOT NULL CHECK (resolution_state IN ('PENDING', 'RESOLVED', 'INSUFFICIENT_DATA', 'AMBIGUOUS_INTRABAR', 'REJECTED')),
    resolution_method text,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outcome_resolver_signal_state_work_idx
    ON platform.outcome_resolver_signal_state(resolution_state, updated_at);

CREATE TABLE IF NOT EXISTS platform.outcome_resolver_lease (
    lease_name text PRIMARY KEY CHECK (lease_name = 'canonical-entry-outcome-resolver'),
    holder_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    acquired_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.outcome_resolver_control (
    control_name text PRIMARY KEY CHECK (control_name = 'canonical-entry-outcome-writer'),
    mode text NOT NULL CHECK (mode IN ('LEGACY_COMPAT', 'RESOLVER_PRIMARY', 'STOPPED')),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    decided_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO platform.outcome_resolver_control(control_name, mode, decided_by)
VALUES ('canonical-entry-outcome-writer', 'LEGACY_COMPAT', 'migration:052')
ON CONFLICT (control_name) DO NOTHING;

COMMENT ON TABLE platform.outcome_resolver_signal_state IS
    'Per-signal durable cursor and unresolved coverage state for the single canonical resolver.';
COMMENT ON TABLE platform.outcome_resolver_lease IS
    'Fencing lease preventing compatibility writers or a restarted resolver from evaluating concurrently.';
COMMENT ON TABLE platform.outcome_resolver_control IS
    'Explicit cutover fence: legacy compatibility writers are disabled before resolver primary ownership.';
