-- Minimal durable V2 risk state. This is deliberately one account/policy canary,
-- not a generalized quota service.
CREATE TABLE IF NOT EXISTS execution_v2.canary_state (
    canary_key text PRIMARY KEY,
    max_new_executions integer NOT NULL CHECK (max_new_executions > 0),
    consumed integer NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS execution_v2.canary_claim (
    canary_key text NOT NULL REFERENCES execution_v2.canary_state(canary_key),
    idempotency_key text NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (canary_key, idempotency_key)
);

CREATE OR REPLACE FUNCTION execution_v2.acquire_canary_slot(
    p_canary_key text, p_idempotency_key text, p_limit integer
) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE current_count integer;
BEGIN
    IF p_limit IS NULL OR p_limit <= 0 THEN RETURN FALSE; END IF;
    INSERT INTO execution_v2.canary_state(canary_key, max_new_executions)
    VALUES (p_canary_key, p_limit)
    ON CONFLICT (canary_key) DO UPDATE
      SET max_new_executions = EXCLUDED.max_new_executions, updated_at = now();
    PERFORM 1 FROM execution_v2.canary_claim
      WHERE canary_key = p_canary_key AND idempotency_key = p_idempotency_key;
    IF FOUND THEN RETURN TRUE; END IF;
    SELECT consumed INTO current_count FROM execution_v2.canary_state
      WHERE canary_key = p_canary_key FOR UPDATE;
    IF current_count >= p_limit THEN RETURN FALSE; END IF;
    INSERT INTO execution_v2.canary_claim(canary_key, idempotency_key)
      VALUES (p_canary_key, p_idempotency_key);
    UPDATE execution_v2.canary_state SET consumed = consumed + 1, updated_at = now()
      WHERE canary_key = p_canary_key;
    RETURN TRUE;
END;
$$;
