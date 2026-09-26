-- Strategy definitions owned by the database (previously the `strategies` array of platform.json).
-- Additive only.
--
-- One row per strategy: identity, adapter, operational routes and enabled flag, plus the
-- strategy-owned trade-management policy block (the same shape scripts/seed_tm_bindings.py reads).
-- Seeded with the strategy the production configuration currently runs, so behaviour is
-- unchanged on upgrade.
CREATE TABLE IF NOT EXISTS platform.strategy_definition (
    strategy_id text PRIMARY KEY CHECK (strategy_id <> ''),
    strategy_version text NOT NULL CHECK (strategy_version <> ''),
    display_name text NOT NULL,
    description text,
    adapter text,
    enabled boolean NOT NULL DEFAULT false,
    routes jsonb NOT NULL DEFAULT '{}'::jsonb,
    trade_management jsonb,
    default_instance_id text,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    CHECK (jsonb_typeof(routes) = 'object'),
    CHECK (trade_management IS NULL OR jsonb_typeof(trade_management) = 'object')
);

INSERT INTO platform.strategy_definition
    (strategy_id, strategy_version, display_name, description, adapter, enabled, routes, default_instance_id, updated_by)
VALUES
    ('CONTEXT_STRUCTURE_RETRACE_V1', 'V1', 'Context Structure Retrace',
     'M15 context setups with a 20% depth retrace entry, structure-capped target and setup-extreme stop.',
     'ContextStructureRetraceAdapter', true,
     '{"audit": true, "shadow_execution": true, "distribution_queue": true}'::jsonb, 'phase6', 'migration:028')
ON CONFLICT (strategy_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON platform.strategy_definition TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON platform.strategy_definition TO trading_readonly;
    END IF;
END
$$;
