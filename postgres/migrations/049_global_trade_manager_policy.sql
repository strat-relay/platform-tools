CREATE TABLE IF NOT EXISTS trade_management.global_policy (
    policy_id text PRIMARY KEY CHECK (policy_id = 'current'),
    policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy) = 'object'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT 'migration:049'
);

INSERT INTO trade_management.global_policy(policy_id) VALUES ('current') ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON trade_management.global_policy TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON trade_management.global_policy TO trading_readonly;
    END IF;
END $$;
