-- Research schema for Context post-exit bar observations.
-- Records are research-only and never affect strategy decisions, sizing, or execution.
-- Writers use ON CONFLICT DO NOTHING (idempotent via record_hash PK).
-- Readers (signals API) look up by signal_id or trade_id.

CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE research.context_post_exit_observations (
    record_hash   text        PRIMARY KEY,
    signal_id     text,
    trade_id      text,
    record_type   text        NOT NULL,
    observed_at   timestamptz,
    payload       jsonb       NOT NULL,
    inserted_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON research.context_post_exit_observations (signal_id) WHERE signal_id IS NOT NULL;
CREATE INDEX ON research.context_post_exit_observations (trade_id)  WHERE trade_id  IS NOT NULL;
