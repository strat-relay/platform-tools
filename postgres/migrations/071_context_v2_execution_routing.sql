-- Enable execution routing for CONTEXT_STRUCTURE_RETRACE_V2.
--
-- Without this, load_config_from_database() synthesises a strategy definition for V2
-- (because no platform.strategy_definition row exists) and hardcodes paper_only=true +
-- shadow_execution=false.  That synthesis path was designed for research-only library
-- strategies; V2 is an execution-eligible strategy and must not fall into it.
--
-- Changes:
--   1. Add platform.strategy_definition row so the synthesis fallback never fires.
--   2. Extend portfolio-shadow-context strategy_ids to include V2 so the execution
--      runtime can size and route V2 signals through the same shadow account as V1.

INSERT INTO platform.strategy_definition (
    strategy_id,
    strategy_version,
    display_name,
    description,
    adapter,
    enabled,
    routes,
    default_instance_id,
    attributes,
    updated_by
)
VALUES (
    'CONTEXT_STRUCTURE_RETRACE_V2',
    'V2',
    'Context Structure Retrace V2',
    'M15 context setups — second-generation evaluator with BTC/multi-asset support.',
    'ContextStructureRetraceAdapter',
    true,
    '{"audit": true, "shadow_execution": true, "distribution_queue": true}'::jsonb,
    NULL,
    '{"execution_eligible": true}'::jsonb,
    'migration:069'
)
ON CONFLICT (strategy_id) DO NOTHING;

-- Extend the shadow portfolio to include V2 alongside V1.
-- Array append is idempotent via the NOT … = ANY guard.
UPDATE platform.orchestration_portfolio
   SET strategy_ids = array_append(strategy_ids, 'CONTEXT_STRUCTURE_RETRACE_V2'),
       revision     = revision + 1,
       updated_at   = now(),
       updated_by   = 'migration:069'
 WHERE portfolio_id = 'portfolio-shadow-context'
   AND NOT ('CONTEXT_STRUCTURE_RETRACE_V2' = ANY(strategy_ids));
