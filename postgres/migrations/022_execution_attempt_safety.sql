-- V2 execution correctness: preserve unresolved attempts and never replay them blindly.
-- This migration adds only an audit/quarantine relation; original attempt rows and states remain
-- unchanged. Existing nonterminal attempts are quarantined at adoption time.
CREATE TABLE IF NOT EXISTS execution_v2.execution_attempt_quarantine (
    attempt_id text PRIMARY KEY REFERENCES execution_v2.execution_attempt(attempt_id),
    reason text NOT NULL CHECK (reason = 'HISTORICAL_AMBIGUOUS_EXECUTION'),
    disposition text NOT NULL CHECK (disposition = 'RECONCILIATION_REQUIRED'),
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO execution_v2.execution_attempt_quarantine(attempt_id, reason, disposition, provenance)
SELECT a.attempt_id, 'HISTORICAL_AMBIGUOUS_EXECUTION', 'RECONCILIATION_REQUIRED',
       jsonb_build_object('source', 'migration_022', 'original_state', a.state)
  FROM execution_v2.execution_attempt a
  LEFT JOIN execution_v2.execution_result r ON r.attempt_id = a.attempt_id
 WHERE a.state IN ('CLAIMED', 'SENDING', 'UNCERTAIN', 'FENCED')
    OR (a.state = 'CONFIRMED' AND r.outcome = 'SUBMITTED'
        AND r.broker_order_id IS NULL AND r.broker_deal_id IS NULL)
ON CONFLICT (attempt_id) DO NOTHING;

ALTER TABLE execution_v2.execution_result
    ADD COLUMN IF NOT EXISTS broker_position_id text;

-- Migration 017 defines a SECURITY INVOKER canary function. Keep the runtime's required
-- privileges declarative instead of relying on manual production ACL repair.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON execution_v2.canary_state TO trading_app;
        GRANT SELECT, INSERT ON execution_v2.canary_claim TO trading_app;
    END IF;
END
$$;
