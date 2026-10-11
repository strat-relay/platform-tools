-- Explicit price evidence for the unified resolver.  Existing theoretical
-- outcomes remain readable; executable outcomes require persisted BID/ASK
-- evidence and never derive quotes from a spread or midpoint.
ALTER TABLE strategy.entry_signal_outcomes
    ADD COLUMN IF NOT EXISTS price_basis text NOT NULL DEFAULT 'THEORETICAL_TOUCH',
    ADD COLUMN IF NOT EXISTS exit_price numeric,
    ADD COLUMN IF NOT EXISTS activation_price numeric,
    ADD COLUMN IF NOT EXISTS outcome_kind text NOT NULL DEFAULT 'STRATEGY_THEORETICAL';

ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_price_basis_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_kind_check;

ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_price_basis_check
        CHECK (price_basis IN ('THEORETICAL_TOUCH', 'EXECUTABLE_BID_ASK')),
    ADD CONSTRAINT entry_signal_outcomes_kind_check
        CHECK (outcome_kind IN ('STRATEGY_THEORETICAL', 'BROKER_REALIZED'));

COMMENT ON COLUMN strategy.entry_signal_outcomes.price_basis IS
    'Price evidence basis. EXECUTABLE_BID_ASK is valid only when the relevant side quotes were persisted.';
COMMENT ON COLUMN strategy.entry_signal_outcomes.outcome_kind IS
    'Theoretical strategy result is separate from broker-realized attribution.';
