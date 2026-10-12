-- Register Context Structure Retrace V2 as a live orchestrator strategy.
--
-- The V2 runner (context_structure_retrace_v2_forward.py) writes state files
-- independently but was never wired into the signal pipeline: no
-- platform.strategy_definition or platform.strategy_instance row existed, so
-- load_adapters() never created a V2 adapter and its signals were never published.
--
-- Also fixes two instrument_membership problems seeded by earlier migrations:
--   - context-v2-research rows had strategy_id 'context_structure_retrace_v2_research'
--     (lowercase, wrong suffix) — not matched by the platform.strategy_instance join.
--   - An orphaned UUID-based instance (3fe0783b-...) had no matching platform row.
--
-- V2 is research-only: shadow_execution=false, distribution_queue=true so signals
-- appear in the canonical ledger but are never routed to the execution worker.
--
-- Idempotent: ON CONFLICT DO NOTHING on inserts; UPDATE is safe to re-run.

INSERT INTO platform.strategy_definition
    (strategy_id, strategy_version, display_name, description, adapter, enabled,
     routes, revision, updated_by, attributes)
VALUES
    ('CONTEXT_STRUCTURE_RETRACE_V2', 'V2',
     'Context Structure Retrace V2',
     'V2 research evaluator — independent runner, paper signals only',
     'ContextStructureRetraceAdapter',
     true,
     '{"audit": true, "shadow_execution": false, "distribution_queue": true}'::jsonb,
     1, 'migration_051', '{"research_only": true}'::jsonb)
ON CONFLICT (strategy_id) DO NOTHING;

INSERT INTO platform.strategy_instance
    (instance_id, strategy_id, display_name, enabled, attributes, revision, updated_by)
VALUES
    ('context-v2-research', 'CONTEXT_STRUCTURE_RETRACE_V2',
     'Context V2 Research', true,
     '{"instance_policy": {"reentry_enabled": true}}'::jsonb,
     1, 'migration_051')
ON CONFLICT (instance_id, strategy_id) DO NOTHING;

-- Fix strategy_id on instrument_membership rows seeded with wrong case/suffix.
UPDATE strategy.instrument_membership
SET strategy_id = 'CONTEXT_STRUCTURE_RETRACE_V2',
    updated_by  = 'migration_051'
WHERE strategy_instance_id = 'context-v2-research'
  AND strategy_id != 'CONTEXT_STRUCTURE_RETRACE_V2';

-- Remove orphaned UUID-based rows that have no matching platform.strategy_instance.
DELETE FROM strategy.instrument_membership
WHERE strategy_instance_id = '3fe0783b-e8fe-4310-ba6c-562055c60ec0';
