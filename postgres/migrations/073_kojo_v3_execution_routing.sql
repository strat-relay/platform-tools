-- Enable shadow execution routing for KOJO_STRUCTURE_RECLAIM_V3.
--
-- Migration 047 registered the strategy with shadow_execution=false.  The
-- forward instance runs ONLINE with execution_mode=SHADOW, so signals arrive
-- with entryType=MARKET, but the execution-v2-runtime never creates intents
-- because:
--   1. platform.strategy_definition.routes has shadow_execution=false
--   2. platform.orchestration_portfolio (portfolio-shadow-context) does not
--      include KOJO_STRUCTURE_RECLAIM_V3 in its strategy_ids array
--
-- This migration corrects both.

-- 1. Flip shadow_execution to true so the execution router accepts the signal.
UPDATE platform.strategy_definition
   SET routes     = '{"audit": true, "shadow_execution": true, "distribution_queue": true}'::jsonb,
       updated_by = 'migration:072',
       updated_at = now()
 WHERE strategy_id = 'KOJO_STRUCTURE_RECLAIM_V3'
   AND (routes->>'shadow_execution')::boolean IS DISTINCT FROM true;

-- 2. Add KOJO_STRUCTURE_RECLAIM_V3 to portfolio-shadow-context so the
--    execution account is in scope for this strategy.
UPDATE platform.orchestration_portfolio
   SET strategy_ids = array_append(strategy_ids, 'KOJO_STRUCTURE_RECLAIM_V3'),
       revision     = revision + 1,
       updated_at   = now(),
       updated_by   = 'migration:072'
 WHERE portfolio_id = 'portfolio-shadow-context'
   AND NOT ('KOJO_STRUCTURE_RECLAIM_V3' = ANY(strategy_ids));
