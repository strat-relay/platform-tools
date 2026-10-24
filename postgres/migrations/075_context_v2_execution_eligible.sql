-- Promote Context V2 to execution-eligible.
--
-- Migration 044 created the strategy_mgmt instance with execution_eligible=false
-- and attributes containing "research_only": true.  Migration 055 created the
-- platform.strategy_definition row with routes.shadow_execution=false and
-- attributes containing "research_only": true.  Migration 071 attempted to fix
-- the definition row but used ON CONFLICT DO NOTHING — the existing row was
-- never updated.
--
-- The execution eligibility gate (execution_v2/eligibility.py) blocks any
-- intent creation when:
--   • strategy_mgmt.strategy_instance_v2.execution_eligible IS NOT TRUE
--   • research_restricted() returns true for instance or parent attributes
--
-- This migration corrects all three blocking conditions:
--   1. Clears research_only from strategy_mgmt.strategy_instance_v2.attributes
--      and sets execution_eligible=true for the context-v2-research instance.
--   2. Clears research_only from platform.strategy_definition.attributes and
--      flips routes.shadow_execution to true for CONTEXT_STRUCTURE_RETRACE_V2.
--
-- All statements are idempotent.

-- 1. strategy_mgmt instance: execution_eligible + clear research_only
UPDATE strategy_mgmt.strategy_instance_v2
   SET execution_eligible = true,
       attributes         = (attributes - 'research_only')
 WHERE attributes->>'instance_id' = 'context-v2-research'
   AND (execution_eligible IS DISTINCT FROM true
        OR (attributes ? 'research_only'));

-- 2. platform parent definition: routes + attributes
UPDATE platform.strategy_definition
   SET routes     = '{"audit": true, "shadow_execution": true, "distribution_queue": true}'::jsonb,
       attributes = '{"execution_eligible": true}'::jsonb,
       updated_by = 'migration:075',
       updated_at = now()
 WHERE strategy_id = 'CONTEXT_STRUCTURE_RETRACE_V2'
   AND (routes->>'shadow_execution' IS DISTINCT FROM 'true'
        OR attributes ? 'research_only');
