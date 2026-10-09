-- Add execution_mode and execution_mode_revision to strategy_mgmt.strategy_instance_v2.
--
-- Separates the two orthogonal strategy-instance control dimensions:
--
--   runtime_state:   OFFLINE (online=false) | ONLINE (online=true)
--                    Controls whether the evaluator generates signals.
--
--   execution_mode:  OFF | SHADOW | LIVE
--                    Controls what happens to those signals.
--                    Operator-switchable at runtime without deployment or restart.
--
-- Valid state pairs:
--   OFFLINE + OFF      → evaluator inactive
--   ONLINE  + OFF      → signals generated, not routed
--   ONLINE  + SHADOW   → signals generated, execution simulated
--   ONLINE  + LIVE     → signals may enter the StratRelay execution pipeline
--
-- The existing `execution_eligible` boolean is NOT changed here.
-- `execution_mode = 'LIVE'` does NOT imply `execution_eligible = true`.
-- LIVE transitions require preflight via the existing execution authority system.
--
-- All changes are additive and backward-compatible.

ALTER TABLE strategy_mgmt.strategy_instance_v2
    ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'OFF'
        CONSTRAINT strategy_instance_v2_execution_mode_valid
        CHECK (execution_mode IN ('OFF', 'SHADOW', 'LIVE')),
    ADD COLUMN IF NOT EXISTS execution_mode_revision INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN strategy_mgmt.strategy_instance_v2.execution_mode IS
    'Operator-controlled execution routing policy: OFF | SHADOW | LIVE. '
    'LIVE requires system-level execution authority preflight. '
    'Independent of runtime_state (online column) and execution_eligible.';

COMMENT ON COLUMN strategy_mgmt.strategy_instance_v2.execution_mode_revision IS
    'Monotonic counter incremented each time execution_mode changes. '
    'Used as execution mode provenance in signal records.';

-- Index for efficient instance lookup by execution_mode (e.g. find all LIVE instances).
CREATE INDEX IF NOT EXISTS idx_strategy_instance_v2_execution_mode
    ON strategy_mgmt.strategy_instance_v2 (execution_mode)
    WHERE execution_mode != 'OFF';

-- Trigger: reject direct SQL attempts to set execution_mode='LIVE' without going through
-- the application-level preflight pathway (the column may only be set LIVE by the
-- application after preflight passes; this adds a DB-level reminder, not a hard block,
-- because the application checks are authoritative).
-- We rely on the application control path for LIVE transition safety.
-- No DB trigger is added here — the application is the authoritative control plane.

INSERT INTO platform.schema_migrations (version) VALUES ('047') ON CONFLICT DO NOTHING;
