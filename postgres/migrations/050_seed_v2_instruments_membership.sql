-- Seed strategy.instrument_membership from strategy_instance_v2.instruments
-- for V2-managed instances that have no membership records yet.
--
-- Migration 047 set instruments directly on the strategy_instance_v2 row (JSONB column).
-- The unified runtime now reads instrument membership from strategy.instrument_membership
-- for all instances (V1 and V2 alike).  This migration seeds the shared table so that
-- existing V2 instances retain their active instruments after the config loader is updated.
--
-- Idempotent: ON CONFLICT DO NOTHING means re-running is harmless.
-- Depends on migration 048 (kojo-v3-forward instance_id attribute patch) for the
-- COALESCE(v.attributes->>'instance_id', ...) to resolve correctly.

INSERT INTO strategy.instrument_membership
    (strategy_instance_id, strategy_id, canonical_instrument, state, revision, updated_by)
SELECT
    COALESCE(v.attributes->>'instance_id', v.id::text)   AS strategy_instance_id,
    CASE sv.evaluator_key
        WHEN 'kojo_structure_reclaim_v3' THEN 'KOJO_STRUCTURE_RECLAIM_V3'
        WHEN 'kojo_structure_reclaim'    THEN 'KOJO_STRUCTURE_RECLAIM'
        ELSE sv.evaluator_key
    END                                                   AS strategy_id,
    upper(entry->>'canonical_instrument')                 AS canonical_instrument,
    'ACTIVE'                                              AS state,
    1                                                     AS revision,
    'migration_050'                                       AS updated_by
FROM strategy_mgmt.strategy_instance_v2 v
JOIN strategy_mgmt.strategy_version sv ON sv.id = v.strategy_version_id
CROSS JOIN LATERAL jsonb_array_elements(v.instruments) AS entry
WHERE jsonb_typeof(v.instruments) = 'array'
  AND entry->>'canonical_instrument' IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM strategy.instrument_membership m
      WHERE m.strategy_instance_id = COALESCE(v.attributes->>'instance_id', v.id::text)
  )
ON CONFLICT (strategy_instance_id, canonical_instrument) DO NOTHING;
