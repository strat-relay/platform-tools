-- Preserve theoretical strategy replay separately from broker execution truth.
-- This is additive; no historical outcome rows are rewritten by the migration.
ALTER TABLE strategy.entry_signal_outcomes
    ADD COLUMN IF NOT EXISTS strategy_outcome text,
    ADD COLUMN IF NOT EXISTS strategy_realized_r numeric,
    ADD COLUMN IF NOT EXISTS strategy_exit_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS execution_outcome text,
    ADD COLUMN IF NOT EXISTS broker_realized_r numeric,
    ADD COLUMN IF NOT EXISTS broker_fill_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS broker_exit_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS broker_exit_reason text,
    ADD COLUMN IF NOT EXISTS attribution_status text NOT NULL DEFAULT 'UNCLASSIFIED',
    ADD COLUMN IF NOT EXISTS attribution_error text;

ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_status_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_check;

ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_status_check
        CHECK (status IN ('OPEN', 'TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'EXPIRED',
                          'INVALIDATED', 'AMBIGUOUS_INTRABAR')),
    ADD CONSTRAINT entry_signal_outcomes_check CHECK (
        (status = 'OPEN' AND realized_r IS NULL AND exit_timestamp IS NULL)
        OR
        (status IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'AMBIGUOUS_INTRABAR')
         AND realized_r IS NOT NULL AND exit_timestamp IS NOT NULL)
        OR
        (status IN ('EXPIRED', 'INVALIDATED') AND realized_r IS NULL AND exit_timestamp IS NOT NULL)
    );

-- Close facts are append-only broker evidence.  They are intentionally separate
-- from execution_result, whose immutable row records the opening submission.
CREATE TABLE IF NOT EXISTS execution_v2.broker_close_fact (
    broker_close_fact_id text PRIMARY KEY,
    entry_signal_id text NOT NULL REFERENCES strategy.entry_signals(signal_id),
    execution_intent_id text REFERENCES execution_v2.execution_intent(execution_intent_id),
    account_id text NOT NULL,
    broker_order_id text,
    broker_deal_id text NOT NULL,
    broker_position_id text,
    symbol text NOT NULL,
    broker_fill_timestamp timestamptz,
    broker_exit_timestamp timestamptz NOT NULL,
    broker_exit_reason text NOT NULL,
    broker_realized_r numeric,
    raw_broker_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, broker_deal_id),
    CHECK (broker_fill_timestamp IS NULL OR broker_exit_timestamp >= broker_fill_timestamp)
);
CREATE INDEX IF NOT EXISTS broker_close_fact_signal_idx
    ON execution_v2.broker_close_fact(entry_signal_id, broker_exit_timestamp);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT ON execution_v2.broker_close_fact TO trading_app;
    END IF;
END
$$;
