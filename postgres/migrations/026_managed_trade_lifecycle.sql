-- ManagedTrade terminal lifecycle (OPEN -> CLOSED) driven by the strategy's canonical outcome.
-- Additive only.
--
-- The strategy owns trade outcome: strategy.entry_signal_outcomes (TARGET_HIT | STOPPED, with
-- exit_timestamp and realized_r). The Trade Manager never computes TP/SL itself; it only
-- consumes that canonical outcome, sets managed_trade.state = 'CLOSED', and records the
-- transition here. Observations, decisions and publication rows are never modified.
CREATE TABLE IF NOT EXISTS trade_management.managed_trade_lifecycle_event (
    lifecycle_event_id text PRIMARY KEY,
    managed_trade_id text NOT NULL REFERENCES trade_management.managed_trade(managed_trade_id),
    entry_signal_id text NOT NULL,
    previous_state text NOT NULL CHECK (previous_state IN ('PENDING_ENTRY', 'OPEN', 'CLOSED', 'CANCELLED')),
    new_state text NOT NULL CHECK (new_state IN ('PENDING_ENTRY', 'OPEN', 'CLOSED', 'CANCELLED')),
    reason text NOT NULL CHECK (reason IN ('STRATEGY_OUTCOME')),
    strategy_outcome text NOT NULL CHECK (strategy_outcome IN ('TARGET_HIT', 'STOPPED')),
    outcome_source text NOT NULL,
    exit_timestamp timestamptz NOT NULL,
    realized_r numeric NOT NULL,
    transitioned_at timestamptz NOT NULL DEFAULT now(),
    CHECK (previous_state <> new_state)
);

-- Idempotency: a trade can be closed at most once, however many reconciliations run.
CREATE UNIQUE INDEX IF NOT EXISTS managed_trade_lifecycle_event_one_close_uq
    ON trade_management.managed_trade_lifecycle_event (managed_trade_id, new_state);

CREATE INDEX IF NOT EXISTS managed_trade_lifecycle_event_transitioned_idx
    ON trade_management.managed_trade_lifecycle_event (transitioned_at);

CREATE OR REPLACE FUNCTION trade_management.forbid_lifecycle_event_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'managed_trade_lifecycle_event is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS managed_trade_lifecycle_event_append_only ON trade_management.managed_trade_lifecycle_event;
CREATE TRIGGER managed_trade_lifecycle_event_append_only
    BEFORE UPDATE OR DELETE ON trade_management.managed_trade_lifecycle_event
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_lifecycle_event_mutation();

-- A CLOSED ManagedTrade never reopens (terminal is terminal).
CREATE OR REPLACE FUNCTION trade_management.forbid_managed_trade_reopen() RETURNS trigger AS $$
BEGIN
    IF OLD.state = 'CLOSED' AND NEW.state IS DISTINCT FROM 'CLOSED' THEN
        RAISE EXCEPTION 'managed_trade % is CLOSED and cannot transition to %', OLD.managed_trade_id, NEW.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS managed_trade_no_reopen ON trade_management.managed_trade;
CREATE TRIGGER managed_trade_no_reopen
    BEFORE UPDATE OF state ON trade_management.managed_trade
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_managed_trade_reopen();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT ON trade_management.managed_trade_lifecycle_event TO trading_app;
        GRANT SELECT ON strategy.entry_signal_outcomes TO trading_app;
    END IF;
END
$$;
