-- Canonical relational V2 risk policy. The policy file is bootstrap/reference material only.
CREATE TABLE IF NOT EXISTS execution_v2.risk_policy (
    policy_id text PRIMARY KEY DEFAULT 'current',
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    version integer NOT NULL CHECK (version > 0),
    enabled boolean NOT NULL,
    risk_per_trade numeric NOT NULL CHECK (risk_per_trade >= 0),
    max_volume numeric NOT NULL CHECK (max_volume >= 0),
    max_signal_age_seconds numeric NOT NULL CHECK (max_signal_age_seconds >= 0),
    max_daily_loss numeric NOT NULL CHECK (max_daily_loss >= 0),
    max_concurrent_positions integer NOT NULL CHECK (max_concurrent_positions >= 0),
    max_concurrent_orders integer NOT NULL CHECK (max_concurrent_orders >= 0),
    max_account_exposure numeric NOT NULL CHECK (max_account_exposure >= 0),
    duplicate_position_policy text NOT NULL,
    canary_max_new_executions integer NOT NULL CHECK (canary_max_new_executions >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    CONSTRAINT risk_policy_singleton CHECK (policy_id = 'current')
);

CREATE TABLE IF NOT EXISTS execution_v2.risk_policy_allowed_account (
    policy_id text NOT NULL REFERENCES execution_v2.risk_policy(policy_id) ON DELETE CASCADE,
    account_id text NOT NULL,
    PRIMARY KEY (policy_id, account_id)
);

CREATE TABLE IF NOT EXISTS execution_v2.risk_policy_allowed_strategy (
    policy_id text NOT NULL REFERENCES execution_v2.risk_policy(policy_id) ON DELETE CASCADE,
    strategy_ref text NOT NULL,
    PRIMARY KEY (policy_id, strategy_ref)
);

CREATE TABLE IF NOT EXISTS execution_v2.risk_policy_allowed_symbol (
    policy_id text NOT NULL REFERENCES execution_v2.risk_policy(policy_id) ON DELETE CASCADE,
    symbol text NOT NULL,
    PRIMARY KEY (policy_id, symbol)
);

CREATE TABLE IF NOT EXISTS execution_v2.risk_policy_change (
    change_id text PRIMARY KEY,
    policy_id text NOT NULL REFERENCES execution_v2.risk_policy(policy_id),
    previous_revision bigint,
    new_revision bigint NOT NULL,
    changed_fields jsonb NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text
);
