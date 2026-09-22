-- P4.2: ManagedTrade, Trade Observation Service, and TM-NONE (architecture/p4-2-managed-trade).
-- Additive only. Does not touch any P2 table's semantics or the legacy `trade_management`
-- schema tables created by 003_future_contracts.sql (`observations`, `decisions`,
-- `checkpoints`) - those are left in place, unused, deprecated (A7 10). SIGNAL_AUTHORITY_MODE
-- and EXECUTION_AUTHORITY_MODE are unaffected: nothing here is a broker-write path.

CREATE TABLE IF NOT EXISTS trade_management.trade_manager_version (
    tm_version_id text PRIMARY KEY,
    evaluator_id text NOT NULL,
    label text NOT NULL,
    manifest jsonb NOT NULL,
    manifest_hash text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('FROZEN', 'RETIRED')),
    frozen_at timestamptz NOT NULL DEFAULT now(),
    derived_from text REFERENCES trade_management.trade_manager_version(tm_version_id),
    CHECK (tm_version_id = 'TMV_' || left(manifest_hash, 24))
);

CREATE OR REPLACE FUNCTION trade_management.forbid_version_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'trade_manager_version rows are immutable and may not be deleted';
    END IF;
    IF NEW.manifest IS DISTINCT FROM OLD.manifest OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash
       OR NEW.tm_version_id IS DISTINCT FROM OLD.tm_version_id OR NEW.frozen_at IS DISTINCT FROM OLD.frozen_at
       OR NEW.evaluator_id IS DISTINCT FROM OLD.evaluator_id THEN
        RAISE EXCEPTION 'trade_manager_version identity/manifest columns are immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status = 'FROZEN' AND NEW.status = 'RETIRED') THEN
        RAISE EXCEPTION 'trade_manager_version.status may only transition FROZEN -> RETIRED';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trade_manager_version_immutable
    BEFORE UPDATE OR DELETE ON trade_management.trade_manager_version
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_version_mutation();

CREATE TABLE IF NOT EXISTS trade_management.tm_version_promotion (
    promotion_id bigserial PRIMARY KEY,
    tm_version_id text NOT NULL REFERENCES trade_management.trade_manager_version(tm_version_id),
    publication_eligibility text NOT NULL CHECK (publication_eligibility IN ('SHADOW_ONLY', 'PUBLISHABLE')),
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    evidence_ref text
);

CREATE TABLE IF NOT EXISTS trade_management.legacy_stream_binding (
    binding_id text PRIMARY KEY,
    strategy_id text NOT NULL,
    strategy_instance_id text,
    instrument text,
    tm_version_id text NOT NULL REFERENCES trade_management.trade_manager_version(tm_version_id),
    valid_from timestamptz NOT NULL,
    binding_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A table-level UNIQUE(...) constraint cannot contain expressions (COALESCE); the equivalent
-- uniqueness rule (A7 10) is expressed as a unique index instead, matching the existing
-- entry_signals_canonical_result_uq convention in 011_p2_a1_signal_contract.sql.
CREATE UNIQUE INDEX IF NOT EXISTS legacy_stream_binding_uq
    ON trade_management.legacy_stream_binding
    (strategy_id, COALESCE(strategy_instance_id, ''), COALESCE(instrument, ''), valid_from);

CREATE OR REPLACE FUNCTION trade_management.forbid_binding_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'legacy_stream_binding is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER legacy_stream_binding_append_only
    BEFORE UPDATE OR DELETE ON trade_management.legacy_stream_binding
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_binding_mutation();

CREATE TABLE IF NOT EXISTS trade_management.managed_trade (
    managed_trade_id text PRIMARY KEY,
    entry_signal_id text NOT NULL UNIQUE REFERENCES strategy.entry_signals(signal_id),
    entry_signal_hash text NOT NULL,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    strategy_ref text NOT NULL,
    parameter_set_ref text,
    parameter_set_status text,
    instrument text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    decision_time timestamptz NOT NULL,
    reference_entry_price numeric,
    initial_stop numeric,
    initial_target numeric,
    risk_distance numeric CHECK (risk_distance IS NULL OR risk_distance > 0),
    tm_version_id text NOT NULL REFERENCES trade_management.trade_manager_version(tm_version_id),
    tm_binding_id text NOT NULL,
    binding_hash text NOT NULL,
    tm_bound_at timestamptz NOT NULL,
    binding_resolution text NOT NULL CHECK (binding_resolution IN ('LEGACY_STATIC', 'DEFAULT_TM_NONE')),
    evidence_mode text NOT NULL CHECK (evidence_mode IN ('FORWARD', 'REPLAY', 'BACKTEST')),
    eligibility text NOT NULL,
    eligibility_reason text,
    creation_lag_seconds numeric,
    record_mode text NOT NULL DEFAULT 'SHADOW' CHECK (record_mode IN ('SHADOW', 'PRIMARY')),
    state text NOT NULL DEFAULT 'OPEN' CHECK (state IN ('PENDING_ENTRY', 'OPEN', 'CLOSED', 'CANCELLED')),
    last_observation_seq bigint NOT NULL DEFAULT 0 CHECK (last_observation_seq >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (managed_trade_id, tm_version_id)
);

CREATE INDEX IF NOT EXISTS managed_trade_strategy_idx ON trade_management.managed_trade(strategy_id, instrument);
CREATE INDEX IF NOT EXISTS managed_trade_state_idx ON trade_management.managed_trade(state);

-- Forbidden-column property (A7 10): no account/ticket/lot/broker-position/execution/
-- entitlement/subscription/customer/published_* column anywhere in this schema. Enforced here
-- as a documented invariant and by tests/test_trade_management_ddl.py against information_schema.

CREATE OR REPLACE FUNCTION trade_management.forbid_managed_trade_binding_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.entry_signal_id IS DISTINCT FROM OLD.entry_signal_id
       OR NEW.entry_signal_hash IS DISTINCT FROM OLD.entry_signal_hash
       OR NEW.strategy_ref IS DISTINCT FROM OLD.strategy_ref
       OR NEW.tm_version_id IS DISTINCT FROM OLD.tm_version_id
       OR NEW.tm_binding_id IS DISTINCT FROM OLD.tm_binding_id
       OR NEW.binding_hash IS DISTINCT FROM OLD.binding_hash
       OR NEW.tm_bound_at IS DISTINCT FROM OLD.tm_bound_at
       OR NEW.decision_time IS DISTINCT FROM OLD.decision_time
       OR NEW.reference_entry_price IS DISTINCT FROM OLD.reference_entry_price
       OR NEW.initial_stop IS DISTINCT FROM OLD.initial_stop
       OR NEW.initial_target IS DISTINCT FROM OLD.initial_target
       OR NEW.evidence_mode IS DISTINCT FROM OLD.evidence_mode THEN
        RAISE EXCEPTION 'managed_trade identity/binding/geometry columns are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER managed_trade_binding_immutable
    BEFORE UPDATE ON trade_management.managed_trade
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_managed_trade_binding_mutation();

CREATE TABLE IF NOT EXISTS trade_management.managed_trade_skip (
    entry_signal_id text PRIMARY KEY,
    reason text NOT NULL,
    detail text,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_management.managed_trade_quarantine (
    entry_signal_id text PRIMARY KEY,
    expected_entry_signal_hash text NOT NULL,
    actual_entry_signal_hash text NOT NULL,
    reason text NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_management.market_snapshot (
    market_snapshot_id text PRIMARY KEY,
    provider_id text NOT NULL,
    feed_id text,
    instrument text NOT NULL,
    source_timestamp timestamptz NOT NULL,
    bid numeric NOT NULL,
    ask numeric NOT NULL,
    spread numeric,
    data_status text NOT NULL CHECK (data_status IN ('FORWARD', 'REPLAY', 'BACKTEST')),
    quote_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ask >= bid)
);

CREATE INDEX IF NOT EXISTS market_snapshot_instrument_idx
    ON trade_management.market_snapshot(instrument, source_timestamp);

CREATE TABLE IF NOT EXISTS trade_management.trade_observation (
    observation_id text PRIMARY KEY,
    managed_trade_id text NOT NULL REFERENCES trade_management.managed_trade(managed_trade_id),
    observation_seq bigint NOT NULL CHECK (observation_seq >= 1),
    market_snapshot_id text NOT NULL REFERENCES trade_management.market_snapshot(market_snapshot_id),
    tm_version_id text NOT NULL REFERENCES trade_management.trade_manager_version(tm_version_id),
    observed_at timestamptz NOT NULL,
    effective_at timestamptz NOT NULL,
    bars_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_status text NOT NULL CHECK (data_status IN ('FORWARD', 'REPLAY', 'BACKTEST')),
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (managed_trade_id, observation_seq)
);

CREATE TABLE IF NOT EXISTS trade_management.trade_manager_decision (
    decision_id text PRIMARY KEY,
    managed_trade_id text NOT NULL,
    tm_version_id text NOT NULL,
    observation_id text NOT NULL REFERENCES trade_management.trade_observation(observation_id),
    observation_seq bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('HOLD', 'MOVE_STOP', 'MOVE_TO_BREAKEVEN', 'TRAIL_STOP',
                                           'PARTIAL_PROFIT', 'EXIT')),
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason_codes text[] NOT NULL DEFAULT '{}',
    decision_trace_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    decision_time timestamptz NOT NULL,
    persisted_at timestamptz NOT NULL DEFAULT now(),
    data_status text NOT NULL CHECK (data_status IN ('FORWARD', 'REPLAY', 'BACKTEST')),
    record_mode text NOT NULL DEFAULT 'SHADOW' CHECK (record_mode IN ('SHADOW', 'PRIMARY')),
    FOREIGN KEY (managed_trade_id, tm_version_id)
        REFERENCES trade_management.managed_trade(managed_trade_id, tm_version_id),
    UNIQUE (observation_id, tm_version_id)
);

CREATE INDEX IF NOT EXISTS trade_manager_decision_trade_idx
    ON trade_management.trade_manager_decision(managed_trade_id, observation_seq);

-- Decisions are immutable once persisted (A6 11 section 2): corrections are new decisions on
-- later observations, never a rewrite of this row.
CREATE OR REPLACE FUNCTION trade_management.forbid_decision_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'trade_manager_decision rows are immutable: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trade_manager_decision_immutable
    BEFORE UPDATE OR DELETE ON trade_management.trade_manager_decision
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_decision_mutation();

-- Publication gate boundary (A6 12). No ManagementSignal or distribution table: this records
-- only the gate's own PUBLISHED/WITHHELD(reason) outcome per decision, so the boundary between
-- TradeManagerDecision and customer-visible publication is exercised without building
-- distribution (mission section 7).
CREATE TABLE IF NOT EXISTS trade_management.publication_decision (
    decision_id text PRIMARY KEY REFERENCES trade_management.trade_manager_decision(decision_id),
    outcome text NOT NULL CHECK (outcome IN ('PUBLISHED', 'WITHHELD')),
    reason text,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (outcome = 'PUBLISHED' OR reason IS NOT NULL)
);
