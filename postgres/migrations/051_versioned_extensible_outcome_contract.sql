-- Outcome Resolver contract v2.
-- Existing rows remain intact; defaults preserve legacy readers during migration.
ALTER TABLE strategy.entry_signal_outcomes
    ADD COLUMN IF NOT EXISTS outcome_contract_version text NOT NULL DEFAULT 'entry-outcome.v2',
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'STRATEGY_REPLAY',
    ADD COLUMN IF NOT EXISTS resolution_state text NOT NULL DEFAULT 'RESOLVED',
    ADD COLUMN IF NOT EXISTS resolution_method text,
    ADD COLUMN IF NOT EXISTS resolution_evidence jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_outcome_type_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_source_check;

ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_outcome_type_v2_check
        CHECK (length(trim(outcome_type)) > 0),
    ADD CONSTRAINT entry_signal_outcomes_source_v2_check
        CHECK (length(trim(source)) > 0),
    ADD CONSTRAINT entry_signal_outcomes_resolution_state_check
        CHECK (resolution_state IN ('RESOLVED', 'INSUFFICIENT_DATA', 'AMBIGUOUS_INTRABAR', 'REJECTED')),
    ADD CONSTRAINT entry_signal_outcomes_source_kind_check
        CHECK (source_kind IN ('STRATEGY_REPLAY', 'BROKER_EXECUTION', 'TRADE_MANAGER', 'OPERATOR'));

UPDATE strategy.entry_signal_outcomes
   SET resolution_state = CASE WHEN status = 'OPEN' THEN 'INSUFFICIENT_DATA' ELSE 'RESOLVED' END
 WHERE resolution_state = 'RESOLVED' AND status = 'OPEN';

CREATE OR REPLACE FUNCTION platform.normalize_outcome_resolution_state()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'OPEN' AND NEW.resolution_state = 'RESOLVED' THEN
        NEW.resolution_state := 'INSUFFICIENT_DATA';
    ELSIF NEW.status <> 'OPEN' AND NEW.resolution_state <> 'AMBIGUOUS_INTRABAR' THEN
        NEW.resolution_state := 'RESOLVED';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS entry_signal_outcomes_resolution_state_normalize
    ON strategy.entry_signal_outcomes;
CREATE TRIGGER entry_signal_outcomes_resolution_state_normalize
BEFORE INSERT OR UPDATE OF status, resolution_state
ON strategy.entry_signal_outcomes
FOR EACH ROW EXECUTE FUNCTION platform.normalize_outcome_resolution_state();

COMMENT ON COLUMN strategy.entry_signal_outcomes.outcome_contract_version IS
    'Versioned resolver contract; readers must branch by this value, not strategy id.';
COMMENT ON COLUMN strategy.entry_signal_outcomes.source_kind IS
    'Provenance of the outcome fact. Strategy replay and broker execution remain separate.';
COMMENT ON COLUMN strategy.entry_signal_outcomes.resolution_state IS
    'Coverage state; unresolved/ambiguous observations remain OPEN and are not invented.';
