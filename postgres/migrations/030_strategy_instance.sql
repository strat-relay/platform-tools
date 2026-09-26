-- Strategy family instances are configuration children of a canonical strategy definition.
-- Instance identity is separate from provider symbols and from the parent strategy identity.
CREATE TABLE IF NOT EXISTS platform.strategy_instance (
    instance_id text PRIMARY KEY CHECK (instance_id <> ''),
    strategy_id text NOT NULL REFERENCES platform.strategy_definition(strategy_id),
    display_name text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    UNIQUE (strategy_id, instance_id),
    CHECK (jsonb_typeof(attributes) = 'object')
);

-- Preserve the existing Context membership/instance identity during the cutover.
INSERT INTO platform.strategy_instance
    (instance_id, strategy_id, display_name, enabled, updated_by)
VALUES
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'Context Structure Retrace phase6', true, 'migration:030')
ON CONFLICT (instance_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON platform.strategy_instance TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON platform.strategy_instance TO trading_readonly;
    END IF;
END
$$;
