-- Candidate only: assign a migration slot after checking the live migration ledger.
-- No canonical signals/outcomes, managed catalog rows, or execution tables touched.
CREATE SCHEMA IF NOT EXISTS research;
CREATE TABLE IF NOT EXISTS research.strategy_library_forward_state (
    run_id text PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research.strategy_library_forward_event (
    run_id text NOT NULL REFERENCES research.strategy_library_forward_state(run_id)
        DEFERRABLE INITIALLY DEFERRED,
    event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('EVALUATION', 'THEORETICAL_OUTCOME')),
    strategy_key text NOT NULL,
    instrument text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL CHECK (payload @> '{"execution_eligible":false}'::jsonb),
    PRIMARY KEY (run_id,event_id)
);
CREATE INDEX IF NOT EXISTS strategy_library_forward_event_stream_idx
    ON research.strategy_library_forward_event(run_id,strategy_key,instrument,observed_at);
