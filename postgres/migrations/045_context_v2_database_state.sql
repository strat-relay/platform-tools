-- Context V2 is database-only.  No runner state, lifecycle event, or signal
-- candidate is persisted to a filesystem artifact.
CREATE SCHEMA IF NOT EXISTS strategy;

CREATE TABLE IF NOT EXISTS strategy.context_v2_runner_state (
    instance_id text PRIMARY KEY,
    strategy_version text NOT NULL,
    state jsonb NOT NULL,
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0,
    runner_status text NOT NULL DEFAULT 'STOPPED',
    last_heartbeat_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(state) = 'object'),
    CHECK (jsonb_typeof(manifest) = 'object')
);

CREATE TABLE IF NOT EXISTS strategy.context_v2_lifecycle_events (
    event_id text PRIMARY KEY,
    instance_id text NOT NULL REFERENCES strategy.context_v2_runner_state(instance_id),
    event_type text NOT NULL,
    event_time timestamptz NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (instance_id, event_type, event_id),
    CHECK (jsonb_typeof(payload) = 'object')
);

CREATE TABLE IF NOT EXISTS strategy.context_v2_signal_candidates (
    signal_id text PRIMARY KEY,
    instance_id text NOT NULL REFERENCES strategy.context_v2_runner_state(instance_id),
    candidate_id text NOT NULL,
    economic_position_id text,
    status text NOT NULL DEFAULT 'DISCOVERED',
    candidate jsonb NOT NULL,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, candidate_id),
    CHECK (jsonb_typeof(candidate) = 'object')
);

CREATE INDEX IF NOT EXISTS context_v2_events_instance_time_idx
    ON strategy.context_v2_lifecycle_events(instance_id, event_time);
CREATE INDEX IF NOT EXISTS context_v2_candidates_instance_status_idx
    ON strategy.context_v2_signal_candidates(instance_id, status);
