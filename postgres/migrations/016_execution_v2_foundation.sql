-- V2 personal execution foundation (architecture/v2-execution). Additive only. Does not touch
-- the legacy `execution` schema (003/008: execution_intents, broker_attempts, orders, fills,
-- reconciliation, intents, broker_state_current) - those remain in place, unused by this work.
-- Does not touch P2 (strategy.entry_signals) or P4.2 (trade_management.*) semantics; the only
-- cross-schema reference is a read-only FK to strategy.entry_signals(signal_id).
--
-- Ownership/generation stays exactly where A5's OD-06 ADR (docs/runtime_boundaries/15) says it
-- must: platform.ownership_leases / platform.acquire_ownership (008), reused unmodified. This
-- migration adds the one function that A5's own evidence (P13) says is missing -
-- assert_generation() - a write-path generation check, which nothing before this migration
-- provided.

CREATE SCHEMA IF NOT EXISTS execution_v2;

-- I1 (docs/runtime_boundaries/04 section 4): "A claim or send transition commits in PostgreSQL
-- only if assert_generation(resource, g) holds inside the same transaction." Raises the same
-- error class acquire_ownership() already uses, so callers handle both identically.
CREATE OR REPLACE FUNCTION platform.assert_generation(p_lease_key text, p_expected_generation bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE current_generation bigint;
BEGIN
    SELECT generation INTO current_generation FROM platform.ownership_leases WHERE lease_key = p_lease_key;
    IF current_generation IS NULL OR current_generation <> p_expected_generation THEN
        RAISE EXCEPTION 'STALE_FENCING_GENERATION for %', p_lease_key USING ERRCODE = '40001';
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS execution_v2.execution_intent (
    execution_intent_id text PRIMARY KEY,
    entry_signal_id text NOT NULL UNIQUE REFERENCES strategy.entry_signals(signal_id),
    entry_signal_hash text NOT NULL,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    strategy_ref text NOT NULL,
    instrument text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    order_type text NOT NULL DEFAULT 'MARKET',
    requested_entry_price numeric,
    stop_price numeric NOT NULL,
    target_price numeric,
    approved_volume numeric NOT NULL CHECK (approved_volume > 0),
    risk_fraction numeric,
    risk_policy_version integer,
    account_id text NOT NULL,
    broker text NOT NULL DEFAULT 'MT5',
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CLAIMED', 'BLOCKED', 'COMPLETED')),
    block_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_intent_account_idx ON execution_v2.execution_intent(account_id, status);

-- Immutable except status/block_reason: identity, provenance and requested geometry are frozen
-- at creation, mirroring trade_management.managed_trade's own binding-immutability convention.
CREATE OR REPLACE FUNCTION execution_v2.forbid_intent_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.entry_signal_id IS DISTINCT FROM OLD.entry_signal_id
       OR NEW.entry_signal_hash IS DISTINCT FROM OLD.entry_signal_hash
       OR NEW.strategy_ref IS DISTINCT FROM OLD.strategy_ref
       OR NEW.instrument IS DISTINCT FROM OLD.instrument
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.stop_price IS DISTINCT FROM OLD.stop_price
       OR NEW.target_price IS DISTINCT FROM OLD.target_price
       OR NEW.approved_volume IS DISTINCT FROM OLD.approved_volume
       OR NEW.account_id IS DISTINCT FROM OLD.account_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'execution_intent identity/geometry columns are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER execution_intent_immutable
    BEFORE UPDATE ON execution_v2.execution_intent
    FOR EACH ROW EXECUTE FUNCTION execution_v2.forbid_intent_mutation();

-- The attempt state machine (docs/runtime_boundaries/15 point 4; 04 section 2.3). One row per
-- execution_intent for this first V2 slice (UNIQUE execution_intent_id) - re-attempting after a
-- proven-safe terminal state (NOT_SENT/FENCED/CANCELLED) is deferred (see
-- docs/v2_execution/README.md "Scope reductions"); duplicate-effect prevention for THIS slice
-- does not require it.
CREATE TABLE IF NOT EXISTS execution_v2.execution_attempt (
    attempt_id text PRIMARY KEY,
    execution_intent_id text NOT NULL UNIQUE REFERENCES execution_v2.execution_intent(execution_intent_id),
    account_id text NOT NULL,
    resource text NOT NULL,
    generation bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('CLAIMED', 'SENDING', 'SENT', 'CONFIRMED', 'REJECTED',
                                        'FAILED', 'FENCED', 'CANCELLED', 'NOT_SENT', 'UNCERTAIN')),
    authorization_id text,
    authorization_exp timestamptz,
    request_fingerprint text,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    sending_at timestamptz,
    terminal_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_attempt_state_idx ON execution_v2.execution_attempt(state);
CREATE INDEX IF NOT EXISTS execution_attempt_resource_idx ON execution_v2.execution_attempt(resource, generation);

-- Decisions/attempts are append-only facts once terminal; a bug found later is a new attempt
-- generation or a reconciliation finding, never a rewrite (mirrors trade_manager_decision's
-- immutability convention). Non-terminal -> terminal transitions remain allowed.
CREATE OR REPLACE FUNCTION execution_v2.forbid_terminal_attempt_mutation() RETURNS trigger AS $$
BEGIN
    IF OLD.state IN ('CONFIRMED', 'REJECTED', 'FAILED', 'FENCED', 'CANCELLED', 'NOT_SENT') THEN
        RAISE EXCEPTION 'execution_attempt % is terminal (%) and immutable', OLD.attempt_id, OLD.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER execution_attempt_terminal_immutable
    BEFORE UPDATE ON execution_v2.execution_attempt
    FOR EACH ROW EXECUTE FUNCTION execution_v2.forbid_terminal_attempt_mutation();

CREATE TABLE IF NOT EXISTS execution_v2.execution_result (
    execution_result_id text PRIMARY KEY,
    attempt_id text NOT NULL UNIQUE REFERENCES execution_v2.execution_attempt(attempt_id),
    execution_intent_id text NOT NULL REFERENCES execution_v2.execution_intent(execution_intent_id),
    outcome text NOT NULL CHECK (outcome IN ('ACCEPTED', 'SUBMITTED', 'FILLED', 'REJECTED', 'BLOCKED',
                                             'UNKNOWN_RECONCILIATION_REQUIRED')),
    account_id text NOT NULL,
    broker_order_id text,
    broker_deal_id text,
    symbol text NOT NULL,
    volume numeric,
    requested_price numeric,
    actual_price numeric,
    submitted_at timestamptz,
    confirmed_at timestamptz,
    raw_broker_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_result_outcome_idx ON execution_v2.execution_result(outcome);

CREATE OR REPLACE FUNCTION execution_v2.forbid_result_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'execution_result rows are immutable: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER execution_result_immutable
    BEFORE UPDATE OR DELETE ON execution_v2.execution_result
    FOR EACH ROW EXECUTE FUNCTION execution_v2.forbid_result_mutation();

-- Resolves an UNKNOWN_RECONCILIATION_REQUIRED result. Append-only: each reconciliation attempt
-- is its own row; the result row itself is never rewritten (mission section 10 "no UNKNOWN
-- broker-write result may automatically become a retry").
CREATE TABLE IF NOT EXISTS execution_v2.reconciliation_finding (
    finding_id text PRIMARY KEY,
    attempt_id text NOT NULL REFERENCES execution_v2.execution_attempt(attempt_id),
    queried_at timestamptz NOT NULL DEFAULT now(),
    broker_truth text NOT NULL CHECK (broker_truth IN ('CONFIRMED_EXECUTED', 'CONFIRMED_NOT_EXECUTED', 'STILL_UNKNOWN')),
    broker_order_id text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS reconciliation_finding_attempt_idx ON execution_v2.reconciliation_finding(attempt_id);

-- Forbidden-column property test companion (tests/test_execution_v2_isolation.py): no column
-- named or typed like a P4/trade_management concept, no ManagementSignal/TradeManagerDecision
-- reference - this schema is intentionally independent of P4.2 (mission section 11).
