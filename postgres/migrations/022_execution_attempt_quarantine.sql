-- Preserve historical execution attempts whose external outcome cannot be proven.
-- This is deliberately additive: the original attempt row and its SENDING state remain
-- unchanged, while the quarantine relation makes the row ineligible for any future
-- dispatch/recovery path.

CREATE TABLE IF NOT EXISTS execution_v2.execution_attempt_quarantine (
    attempt_id text PRIMARY KEY REFERENCES execution_v2.execution_attempt(attempt_id),
    reason text NOT NULL CHECK (reason = 'HISTORICAL_AMBIGUOUS_EXECUTION'),
    disposition text NOT NULL CHECK (disposition = 'RECONCILIATION_REQUIRED'),
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS execution_attempt_quarantine_disposition_idx
    ON execution_v2.execution_attempt_quarantine(disposition, quarantined_at);

-- The current production baseline has 28 unresolved SENDING rows. Backfill only those
-- historical rows; future rows are never implicitly quarantined by this migration.
INSERT INTO execution_v2.execution_attempt_quarantine
    (attempt_id, reason, disposition, provenance)
SELECT attempt_id,
       'HISTORICAL_AMBIGUOUS_EXECUTION',
       'RECONCILIATION_REQUIRED',
       jsonb_build_object(
           'source', 'execution_v2_historical_attempt_reconciliation',
           'original_state', state,
           'original_created_at', created_at,
           'original_generation', generation
       )
FROM execution_v2.execution_attempt
WHERE state = 'SENDING'
ON CONFLICT (attempt_id) DO NOTHING;
