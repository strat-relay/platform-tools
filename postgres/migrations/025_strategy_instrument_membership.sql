-- Instance-scoped canonical instrument membership.
-- Provider symbols belong to provider mappings/catalogues, never to this table.

CREATE TABLE IF NOT EXISTS strategy.instrument_membership (
    strategy_instance_id text NOT NULL,
    strategy_id text NOT NULL,
    canonical_instrument text NOT NULL,
    state text NOT NULL CHECK (state IN ('ACTIVE', 'DISABLED')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    PRIMARY KEY (strategy_instance_id, canonical_instrument)
);

CREATE INDEX IF NOT EXISTS instrument_membership_instance_state_idx
    ON strategy.instrument_membership(strategy_instance_id, state, canonical_instrument);

-- The existing Context forward runner's four configured provider symbols are
-- seeded as canonical identities.  This is an additive, idempotent cutover;
-- the provider mapping remains in platform.json and is not copied here.
INSERT INTO strategy.instrument_membership
    (strategy_instance_id, strategy_id, canonical_instrument, state, revision, updated_by)
VALUES
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'XAUUSD', 'ACTIVE', 1, 'migration:025'),
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'BTCUSD', 'ACTIVE', 1, 'migration:025'),
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'USDJPY', 'ACTIVE', 1, 'migration:025'),
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'EURUSD', 'ACTIVE', 1, 'migration:025')
ON CONFLICT (strategy_instance_id, canonical_instrument) DO NOTHING;
