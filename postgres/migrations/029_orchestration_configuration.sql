-- Signal orchestrator configuration owned by the database (the remainder of platform.json).
-- Additive only.
--
-- runtime_setting holds the top-level scalar/object settings (execution_mode, mcp_url,
-- execution_mcp_url, execution_transport_verified, sizing_scenarios, schema_version and the
-- legacy demo_* keys). Accounts and portfolios get their own tables. Strategies come from
-- platform.strategy_definition (028); instrument mappings from platform.instrument_provider_mapping
-- (027).
--
-- Deliberately NOT seeded: these values are deployment-specific (bridge endpoints, account
-- references). scripts/import_platform_config.py imports the running configuration once; until
-- then the orchestrator refuses to start from an empty configuration (fail closed).
CREATE TABLE IF NOT EXISTS platform.runtime_setting (
    key text PRIMARY KEY CHECK (key <> '' AND key NOT IN ('accounts', 'portfolios', 'strategies', 'symbol_mappings')),
    value jsonb NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.orchestration_account (
    account_id text PRIMARY KEY CHECK (account_id <> ''),
    broker text NOT NULL,
    broker_environment text NOT NULL,
    broker_account_reference text,
    currency text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    execution_mode text NOT NULL DEFAULT 'SHADOW',
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.orchestration_portfolio (
    portfolio_id text PRIMARY KEY CHECK (portfolio_id <> ''),
    name text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    base_currency text NOT NULL,
    sizing_policy_id text,
    account_ids text[] NOT NULL DEFAULT '{}',
    strategy_ids text[] NOT NULL DEFAULT '{}',
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

-- Lossless import of per-strategy keys that have no dedicated column (e.g. portfolio_routing,
-- visibility, status); load_config() merges them back into the strategy record.
ALTER TABLE platform.strategy_definition ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON platform.runtime_setting, platform.orchestration_account,
            platform.orchestration_portfolio TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON platform.runtime_setting, platform.orchestration_account,
            platform.orchestration_portfolio TO trading_readonly;
    END IF;
END
$$;
