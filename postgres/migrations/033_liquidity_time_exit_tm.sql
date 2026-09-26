-- Generic Trade Manager lifecycle now accepts the strategy-owned TIME_EXIT outcome.
-- The Trade Manager still only consumes the outcome; it does not calculate expiry.
ALTER TABLE trade_management.managed_trade_lifecycle_event
    DROP CONSTRAINT IF EXISTS managed_trade_lifecycle_event_strategy_outcome_check;

ALTER TABLE trade_management.managed_trade_lifecycle_event
    ADD CONSTRAINT managed_trade_lifecycle_event_strategy_outcome_check
    CHECK (strategy_outcome IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT'));
