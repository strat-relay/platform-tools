CREATE TABLE IF NOT EXISTS orchestration.signals (
    signal_id text PRIMARY KEY, strategy_id text NOT NULL, symbol text NOT NULL, direction text,
    signal_time timestamptz, payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS execution.execution_intents (
    intent_id text PRIMARY KEY, signal_id text REFERENCES orchestration.signals(signal_id),
    economic_position_id text, mode text NOT NULL DEFAULT 'paper', status text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS execution.broker_attempts (
    attempt_id text PRIMARY KEY, intent_id text REFERENCES execution.execution_intents(intent_id),
    broker text, idempotency_key text UNIQUE, status text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS execution.orders (
    order_id text PRIMARY KEY, attempt_id text REFERENCES execution.broker_attempts(attempt_id),
    broker_order_id text UNIQUE, status text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS execution.fills (
    fill_id text PRIMARY KEY, order_id text REFERENCES execution.orders(order_id),
    broker_fill_id text UNIQUE, quantity numeric, price numeric, filled_at timestamptz, payload jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS execution.reconciliation (
    reconciliation_id text PRIMARY KEY, broker text NOT NULL, as_of timestamptz NOT NULL,
    status text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS trade_management.observations (
    observation_id text PRIMARY KEY, source text NOT NULL, observed_at timestamptz, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_management.decisions (
    decision_id text PRIMARY KEY, observation_id text REFERENCES trade_management.observations(observation_id),
    decision_type text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS trade_management.checkpoints (
    checkpoint_id text PRIMARY KEY, stream_name text NOT NULL, state_hash text NOT NULL,
    payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit.execution_safety_invariants (
    invariant_id text PRIMARY KEY, description text NOT NULL, enforced_by text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
INSERT INTO audit.execution_safety_invariants(invariant_id, description, enforced_by) VALUES
 ('NO_BROKER_WRITES_PHASE6_IMPORT', 'Phase 6 import and validation must not call broker APIs or OrderSend.', 'process boundary and code review'),
 ('NO_LIVE_CUTOVER_PHASE6_IMPORT', 'Database import is additive; the live runner remains file-backed until explicit approval.', 'deployment procedure')
ON CONFLICT (invariant_id) DO NOTHING;
