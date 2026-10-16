-- Durable operator history for strategy-instrument membership decisions.
-- This is an audit relation only; it does not change membership semantics.
CREATE TABLE IF NOT EXISTS strategy.instrument_membership_audit (
    audit_id bigserial PRIMARY KEY,
    strategy_instance_id text NOT NULL,
    strategy_id text NOT NULL,
    canonical_instrument text NOT NULL,
    previous_state text,
    new_state text NOT NULL CHECK (new_state IN ('ACTIVE', 'DISABLED')),
    actor text NOT NULL,
    reason text NOT NULL,
    selected_period text,
    provenance_filter text,
    strategy_version text,
    parameter_set_ref text,
    performance_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS instrument_membership_audit_lookup_idx
    ON strategy.instrument_membership_audit(strategy_instance_id, canonical_instrument, created_at DESC);
