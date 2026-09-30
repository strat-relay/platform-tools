-- Operator invalidation of stale strategy signals (additive).
--
-- An operator may mark a signal's canonical outcome INVALIDATED (no realized R, an exit time,
-- excluded from win rate/expectancy). The override remains an append-only audit record; the
-- Context runner is authoritative for the projected outcome row and may reconcile it later.
CREATE TABLE IF NOT EXISTS strategy.entry_signal_outcome_override (
    override_id text PRIMARY KEY,
    signal_id text NOT NULL REFERENCES strategy.entry_signals(signal_id),
    previous_status text,                      -- NULL: the signal had no outcome row
    new_status text NOT NULL CHECK (new_status IN ('INVALIDATED')),
    reason text NOT NULL CHECK (reason <> ''),
    operator text NOT NULL CHECK (operator <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (signal_id, new_status)
);

CREATE OR REPLACE FUNCTION strategy.forbid_outcome_override_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'entry_signal_outcome_override is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entry_signal_outcome_override_append_only ON strategy.entry_signal_outcome_override;
CREATE TRIGGER entry_signal_outcome_override_append_only
    BEFORE UPDATE OR DELETE ON strategy.entry_signal_outcome_override
    FOR EACH ROW EXECUTE FUNCTION strategy.forbid_outcome_override_mutation();

-- The Trade Manager closes a managed trade on an INVALIDATED outcome too; it carries no R.
ALTER TABLE trade_management.managed_trade_lifecycle_event
    DROP CONSTRAINT IF EXISTS managed_trade_lifecycle_event_strategy_outcome_check;
ALTER TABLE trade_management.managed_trade_lifecycle_event
    ADD CONSTRAINT managed_trade_lifecycle_event_strategy_outcome_check
    CHECK (strategy_outcome IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'INVALIDATED'));
ALTER TABLE trade_management.managed_trade_lifecycle_event ALTER COLUMN realized_r DROP NOT NULL;
ALTER TABLE trade_management.managed_trade_lifecycle_event
    DROP CONSTRAINT IF EXISTS managed_trade_lifecycle_event_realized_r_required;
ALTER TABLE trade_management.managed_trade_lifecycle_event
    ADD CONSTRAINT managed_trade_lifecycle_event_realized_r_required
    CHECK (strategy_outcome = 'INVALIDATED' OR realized_r IS NOT NULL);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT ON strategy.entry_signal_outcome_override TO trading_app;
        GRANT SELECT, INSERT, UPDATE ON strategy.entry_signal_outcomes TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON strategy.entry_signal_outcome_override TO trading_readonly;
    END IF;
END
$$;
