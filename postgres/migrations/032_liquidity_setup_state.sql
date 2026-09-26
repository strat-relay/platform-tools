-- Durable pre-entry setup lifecycle. This relation never represents a trade;
-- post-entry outcomes remain in strategy.entry_signal_outcomes.
CREATE TABLE IF NOT EXISTS strategy.liquidity_setup_state (
    setup_id text PRIMARY KEY,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    instance_id text NOT NULL,
    canonical_instrument text NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING_RETRACE', 'ENTERED', 'EXPIRED', 'INVALIDATED')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS liquidity_setup_state_active_idx
    ON strategy.liquidity_setup_state(instance_id, canonical_instrument, state);
