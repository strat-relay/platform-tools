-- Clock observations carry no market price. Never fabricate a quote for a deadline.
ALTER TABLE trade_management.trade_observation
    ALTER COLUMN market_snapshot_id DROP NOT NULL;
ALTER TABLE trade_management.trade_observation
    ADD CONSTRAINT clock_observation_requires_deadline
    CHECK (market_snapshot_id IS NOT NULL OR COALESCE(bars_ref->>'kind' = 'THEORETICAL_DEADLINE', false));

-- Existing deadlines were anchored to broker fills. Recompute from strategy evidence
-- in the scheduler; do not rewrite frozen bindings, policies, decisions or outcomes.
UPDATE trade_management.managed_trade
   SET time_exit_at = NULL
 WHERE state = 'OPEN' AND time_exit_minutes IS NOT NULL;
