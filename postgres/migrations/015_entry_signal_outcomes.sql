-- Canonical lifecycle projection for Context Strategy ENTRY_ONLY outcomes.
-- Strategy decision logic remains authoritative; this relation only stores
-- the current outcome projection for an existing immutable EntrySignal.
CREATE TABLE IF NOT EXISTS strategy.entry_signal_outcomes (
    signal_id text PRIMARY KEY REFERENCES strategy.entry_signals(signal_id),
    outcome_type text NOT NULL CHECK (outcome_type = 'ENTRY_ONLY'),
    status text NOT NULL CHECK (status IN ('OPEN', 'TARGET_HIT', 'STOPPED')),
    realized_r numeric,
    exit_timestamp timestamptz,
    source text NOT NULL CHECK (source = 'CONTEXT_STRUCTURE_RETRACE_V1'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'OPEN' AND realized_r IS NULL AND exit_timestamp IS NULL)
        OR
        (status IN ('TARGET_HIT', 'STOPPED') AND realized_r IS NOT NULL AND exit_timestamp IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS entry_signal_outcomes_status_idx
    ON strategy.entry_signal_outcomes(status, updated_at DESC);
