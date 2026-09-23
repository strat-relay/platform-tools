-- Durable store for an operator-edited V2 risk policy override (mission
-- CLAUDE-V2-RISK-EXECUTION-CONSOLE). A single-row singleton (id is always 'current') holding the
-- full policy document as validated JSON - execution_v2/risk_policy_store.py is the only writer,
-- and only ever writes a document that has already passed the exact same validation
-- execution_v2/risk.py's load_risk_policy() applies to the file-based baseline.
--
-- This table does NOT change what the execution_v2 runtime actually reads today: the runtime
-- (execution_v2/runtime/service.py) still loads orchestration/config/v2_execution_risk_policy.json
-- exactly as before, unchanged by this migration. This table is a persistence layer for the new
-- Console control surface only - wiring the runtime to prefer this override is an explicit,
-- separately-authorized future step (see docs/console_v2_risk_execution.md), not something this
-- migration or the mutation endpoint does on its own.
CREATE TABLE IF NOT EXISTS execution_v2.risk_policy_override (
    id text PRIMARY KEY DEFAULT 'current',
    policy_json jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    CONSTRAINT risk_policy_override_singleton CHECK (id = 'current')
);
