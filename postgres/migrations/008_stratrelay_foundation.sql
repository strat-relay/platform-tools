-- StratRelay V1.2 foundation.  This migration adds durable coordination,
-- canonical evaluation history, transactional outbox/inbox, and execution
-- idempotency.  Existing phase6 tables remain historical compatibility data.
CREATE TABLE IF NOT EXISTS platform.reason_codes (
    code text NOT NULL,
    version text NOT NULL,
    category text NOT NULL,
    description text NOT NULL,
    terminal boolean NOT NULL DEFAULT true,
    PRIMARY KEY (code, version)
);

CREATE TABLE IF NOT EXISTS strategy.evaluations (
    evaluation_id text PRIMARY KEY,
    strategy_id text NOT NULL,
    strategy_version_id text REFERENCES platform.strategy_versions(strategy_version_id),
    parameter_set_id text,
    instrument text NOT NULL,
    direction text,
    decision_time timestamptz NOT NULL,
    candidate_id text,
    decision text NOT NULL CHECK (decision IN ('SIGNAL', 'REJECT', 'NO_CANDIDATE', 'REVIEW_REQUIRED')),
    trace_fidelity text NOT NULL CHECK (trace_fidelity IN ('L0', 'L1', 'L2', 'L3')),
    runtime_version text NOT NULL,
    evaluator_version text NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    canonical_payload jsonb NOT NULL,
    canonical_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategy.decision_traces (
    evaluation_id text PRIMARY KEY REFERENCES strategy.evaluations(evaluation_id) ON DELETE CASCADE,
    trace_version text NOT NULL,
    trace_hash text NOT NULL UNIQUE,
    canonical_payload jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy.evaluation_reason_codes (
    evaluation_id text NOT NULL REFERENCES strategy.evaluations(evaluation_id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    code text NOT NULL,
    version text NOT NULL,
    PRIMARY KEY (evaluation_id, ordinal),
    FOREIGN KEY (code, version) REFERENCES platform.reason_codes(code, version)
);

CREATE TABLE IF NOT EXISTS strategy.stage_results (
    evaluation_id text NOT NULL REFERENCES strategy.evaluations(evaluation_id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    stage_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('PASS', 'FAIL', 'NOT_EVALUATED')),
    primitive_id text,
    observed jsonb,
    expected jsonb,
    margin jsonb,
    evidence_times jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason_code text,
    reason_version text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (evaluation_id, ordinal),
    FOREIGN KEY (reason_code, reason_version) REFERENCES platform.reason_codes(code, version)
);

CREATE TABLE IF NOT EXISTS strategy.candidates (
    candidate_id text PRIMARY KEY,
    strategy_id text NOT NULL,
    strategy_version_id text REFERENCES platform.strategy_versions(strategy_version_id),
    instrument text NOT NULL,
    detected_at timestamptz NOT NULL,
    status text NOT NULL,
    source_event_id text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    canonical_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategy.signals (
    signal_id text PRIMARY KEY,
    evaluation_id text REFERENCES strategy.evaluations(evaluation_id),
    candidate_id text REFERENCES strategy.candidates(candidate_id),
    strategy_id text NOT NULL,
    instrument text NOT NULL,
    direction text,
    signal_time timestamptz NOT NULL,
    payload jsonb NOT NULL,
    canonical_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.runtime_instances (
    instance_id text PRIMARY KEY,
    component text NOT NULL,
    process_id bigint,
    status text NOT NULL CHECK (status IN ('STARTING', 'RUNNING', 'STOPPING', 'STOPPED', 'FAILED')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    stopped_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS platform.ownership_leases (
    lease_key text PRIMARY KEY,
    holder_instance_id text NOT NULL REFERENCES platform.runtime_instances(instance_id),
    generation bigint NOT NULL CHECK (generation > 0),
    acquired_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    UNIQUE (lease_key, generation)
);

CREATE OR REPLACE FUNCTION platform.acquire_ownership(
    p_lease_key text, p_holder_instance_id text, p_expected_generation bigint
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE next_generation bigint;
BEGIN
    SELECT generation + 1 INTO next_generation
      FROM platform.ownership_leases
     WHERE lease_key = p_lease_key AND generation = p_expected_generation
     FOR UPDATE;
    IF next_generation IS NULL THEN
        IF EXISTS (SELECT 1 FROM platform.ownership_leases WHERE lease_key = p_lease_key) THEN
            RAISE EXCEPTION 'STALE_FENCING_GENERATION for %', p_lease_key USING ERRCODE = '40001';
        END IF;
        next_generation := 1;
        INSERT INTO platform.ownership_leases(lease_key, holder_instance_id, generation)
        VALUES (p_lease_key, p_holder_instance_id, next_generation)
        ON CONFLICT (lease_key) DO UPDATE SET holder_instance_id = EXCLUDED.holder_instance_id,
            generation = platform.ownership_leases.generation + 1,
            acquired_at = now(), heartbeat_at = now()
        RETURNING generation INTO next_generation;
        RETURN next_generation;
    END IF;
    UPDATE platform.ownership_leases
       SET holder_instance_id = p_holder_instance_id, generation = next_generation,
           acquired_at = now(), heartbeat_at = now()
     WHERE lease_key = p_lease_key AND generation = p_expected_generation;
    RETURN next_generation;
END;
$$;

CREATE TABLE IF NOT EXISTS platform.outbox_events (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    aggregate_version bigint,
    schema_version text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    correlation_id text,
    causation_id text,
    publish_status text NOT NULL DEFAULT 'PENDING' CHECK (publish_status IN ('PENDING', 'PUBLISHED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON platform.outbox_events(created_at) WHERE publish_status <> 'PUBLISHED';

CREATE TABLE IF NOT EXISTS platform.inbox_events (
    consumer_name text NOT NULL,
    event_id text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    status text NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED', 'PROCESSED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error text,
    PRIMARY KEY (consumer_name, event_id)
);

CREATE TABLE IF NOT EXISTS execution.intents (
    intent_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    intent_type text NOT NULL,
    aggregate_id text,
    status text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS execution.results (
    result_id text PRIMARY KEY,
    intent_id text NOT NULL REFERENCES execution.intents(intent_id),
    broker_operation_id text,
    status text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (intent_id, broker_operation_id)
);

CREATE TABLE IF NOT EXISTS execution.broker_state_current (
    account_id text NOT NULL,
    broker_position_id text NOT NULL,
    symbol text,
    direction text,
    volume numeric,
    state_version bigint NOT NULL DEFAULT 0,
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (account_id, broker_position_id)
);

CREATE TABLE IF NOT EXISTS execution.broker_state_history (
    observation_id text PRIMARY KEY,
    account_id text NOT NULL,
    broker_position_id text,
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);
