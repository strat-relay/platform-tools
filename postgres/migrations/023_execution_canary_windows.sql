-- Durable, explicit execution canary generations.  Existing claims remain attached to
-- their original fixed key and are never rewritten.
ALTER TABLE execution_v2.canary_state
    ADD COLUMN IF NOT EXISTS account_id text,
    ADD COLUMN IF NOT EXISTS environment text,
    ADD COLUMN IF NOT EXISTS generation bigint,
    ADD COLUMN IF NOT EXISTS lifecycle_state text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz,
    ADD COLUMN IF NOT EXISTS opened_by text;

UPDATE execution_v2.canary_state
SET environment = COALESCE(environment, split_part(canary_key, ':', 2)),
    account_id = COALESCE(account_id, split_part(canary_key, ':', 3)),
    generation = COALESCE(generation, 1),
    lifecycle_state = COALESCE(lifecycle_state,
      CASE WHEN consumed >= max_new_executions THEN 'EXHAUSTED' ELSE 'ACTIVE' END),
    created_at = COALESCE(created_at, updated_at, now()),
    opened_by = COALESCE(opened_by, 'migration:023')
WHERE environment IS NULL OR account_id IS NULL OR generation IS NULL
   OR lifecycle_state IS NULL OR created_at IS NULL OR opened_by IS NULL;

ALTER TABLE execution_v2.canary_state
    ALTER COLUMN account_id SET NOT NULL,
    ALTER COLUMN environment SET NOT NULL,
    ALTER COLUMN generation SET NOT NULL,
    ALTER COLUMN lifecycle_state SET NOT NULL,
    ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE execution_v2.canary_state
    ADD CONSTRAINT canary_state_lifecycle_check
    CHECK (lifecycle_state IN ('ACTIVE', 'EXHAUSTED', 'CLOSED'));

CREATE UNIQUE INDEX IF NOT EXISTS canary_state_one_active_per_account
    ON execution_v2.canary_state(environment, account_id)
    WHERE lifecycle_state = 'ACTIVE';

CREATE TABLE IF NOT EXISTS execution_v2.canary_window_change (
    change_id text PRIMARY KEY,
    canary_key text NOT NULL REFERENCES execution_v2.canary_state(canary_key),
    previous_canary_key text,
    environment text NOT NULL,
    account_id text NOT NULL,
    generation bigint NOT NULL,
    max_new_executions integer NOT NULL CHECK (max_new_executions > 0),
    action text NOT NULL CHECK (action IN ('OPEN', 'CLOSE')),
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text NOT NULL
);

CREATE OR REPLACE FUNCTION execution_v2.acquire_active_canary_slot(
    p_environment text, p_account_id text, p_idempotency_key text, p_limit integer
) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE window_row execution_v2.canary_state%ROWTYPE;
BEGIN
    IF p_limit IS NULL OR p_limit <= 0 THEN RETURN FALSE; END IF;
    SELECT * INTO window_row
      FROM execution_v2.canary_state
     WHERE environment = p_environment AND account_id = p_account_id
       AND lifecycle_state = 'ACTIVE'
     ORDER BY generation DESC
     FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF EXISTS (SELECT 1 FROM execution_v2.canary_claim
                WHERE canary_key = window_row.canary_key
                  AND idempotency_key = p_idempotency_key) THEN
        RETURN TRUE;
    END IF;
    IF window_row.consumed >= LEAST(window_row.max_new_executions, p_limit) THEN
        UPDATE execution_v2.canary_state SET lifecycle_state = 'EXHAUSTED', closed_at = now(), updated_at = now()
         WHERE canary_key = window_row.canary_key;
        RETURN FALSE;
    END IF;
    INSERT INTO execution_v2.canary_claim(canary_key, idempotency_key)
      VALUES (window_row.canary_key, p_idempotency_key);
    UPDATE execution_v2.canary_state
       SET consumed = consumed + 1,
           lifecycle_state = CASE WHEN consumed + 1 >= max_new_executions THEN 'EXHAUSTED' ELSE 'ACTIVE' END,
           closed_at = CASE WHEN consumed + 1 >= max_new_executions THEN now() ELSE closed_at END,
           updated_at = now()
     WHERE canary_key = window_row.canary_key;
    RETURN TRUE;
END;
$$;

GRANT SELECT, INSERT, UPDATE ON execution_v2.canary_state TO trading_app;
GRANT SELECT, INSERT ON execution_v2.canary_claim TO trading_app;
GRANT SELECT, INSERT ON execution_v2.canary_window_change TO trading_app;
