-- Patch: add instance_id to Kojo V3 Forward strategy_instance_v2 attributes.
--
-- Migration 047 registered the instance but omitted instance_id from the
-- attributes JSONB.  The orchestration config loader uses:
--
--   (attrs_json or {}).get("instance_id") or inst_id
--
-- Without instance_id in attributes it falls back to the row UUID, which the
-- frontend instance detail page cannot resolve to a named KOJO instance.
--
-- This migration is idempotent: the || operator merges only the new key;
-- if instance_id is already present the value is overwritten with the same
-- string, so re-running is safe.

UPDATE strategy_mgmt.strategy_instance_v2
SET attributes = COALESCE(attributes, '{}'::jsonb) || '{"instance_id": "kojo-v3-forward"}'::jsonb
WHERE id = 'e5f7a9b1-c3d5-4e7f-a1b3-5c7e9f1b3d5e';
