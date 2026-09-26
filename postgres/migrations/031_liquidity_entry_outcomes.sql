-- Extend the shared outcome relation for strategy-owned Liquidity lifecycles.
-- Existing Context rows retain ENTRY_ONLY/OPEN/TARGET_HIT/STOPPED semantics.
ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_outcome_type_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_status_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_source_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_check;

ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_outcome_type_check
        CHECK (outcome_type IN ('ENTRY_ONLY', 'LIQUIDITY_ENTRY')),
    ADD CONSTRAINT entry_signal_outcomes_status_check
        CHECK (status IN ('OPEN', 'TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'EXPIRED', 'INVALIDATED')),
    ADD CONSTRAINT entry_signal_outcomes_source_check
        CHECK (source IN ('CONTEXT_STRUCTURE_RETRACE_V1', 'LIQUIDITY_DISPLACEMENT_SCALP_V1')),
    ADD CONSTRAINT entry_signal_outcomes_check CHECK (
        (status = 'OPEN' AND realized_r IS NULL AND exit_timestamp IS NULL)
        OR
        (status IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT') AND realized_r IS NOT NULL AND exit_timestamp IS NOT NULL)
        OR
        (status IN ('EXPIRED', 'INVALIDATED') AND realized_r IS NULL AND exit_timestamp IS NOT NULL)
    );
