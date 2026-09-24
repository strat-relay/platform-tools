-- Durable per-signal V2 execution audit extensions.
-- The execution_intent row remains the canonical evaluation record. This migration only
-- broadens its account key and stores supplemental evaluator evidence.

ALTER TABLE execution_v2.execution_intent
    DROP CONSTRAINT IF EXISTS execution_intent_entry_signal_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS execution_intent_signal_account_uidx
    ON execution_v2.execution_intent(entry_signal_id, account_id);

CREATE TABLE IF NOT EXISTS execution_v2.execution_risk_evidence (
    execution_intent_id text PRIMARY KEY REFERENCES execution_v2.execution_intent(execution_intent_id),
    policy_version integer,
    policy_fingerprint text,
    risk_per_trade numeric,
    account_equity numeric,
    risk_budget_usd numeric,
    stop_distance numeric,
    broker_volume_min numeric,
    broker_volume_step numeric,
    broker_volume_max numeric,
    calculated_volume numeric,
    submitted_volume numeric,
    estimated_loss_usd numeric,
    daily_loss_used numeric,
    concurrent_positions_used integer,
    concurrent_orders_used integer,
    signal_age_seconds numeric,
    max_signal_age_seconds numeric,
    canary_consumed integer,
    canary_max integer,
    decision_reason text,
    diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_risk_evidence_policy_idx
    ON execution_v2.execution_risk_evidence(policy_version, created_at DESC);

INSERT INTO platform.system_metadata(key, value)
VALUES ('execution.audit_cutoff', jsonb_build_object('cutoff_utc', now(), 'schema_version', '021'))
ON CONFLICT (key) DO NOTHING;
