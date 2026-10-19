-- Explicit operator commands may exit without market data. BE still validates a fresh quote.
ALTER TABLE trade_management.trade_observation DROP CONSTRAINT clock_observation_requires_deadline;
ALTER TABLE trade_management.trade_observation ADD CONSTRAINT clock_observation_requires_deadline
CHECK (market_snapshot_id IS NOT NULL OR COALESCE(bars_ref->>'kind' IN ('THEORETICAL_DEADLINE', 'OPERATOR_ACTION'), false));
