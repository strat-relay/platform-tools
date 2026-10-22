-- Scheduling hints only; no strategy registrations, grants or execution changes.
CREATE TABLE platform.strategy_runtime_outbox (
    event_id text PRIMARY KEY CHECK (event_id ~ '^[0-9a-f]{64}$'),
    runtime_id text NOT NULL CHECK (runtime_id ~ '^[a-zA-Z0-9_-]{1,64}$'),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz
);
CREATE INDEX strategy_runtime_pending_idx ON platform.strategy_runtime_outbox(created_at,event_id)
    WHERE published_at IS NULL;
CREATE INDEX strategy_runtime_delivered_idx ON platform.strategy_runtime_outbox(published_at,event_id)
    WHERE published_at IS NOT NULL;
CREATE FUNCTION platform.strategy_runtime_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('strategy_runtime_outbox', '');
    RETURN NEW;
END;
$$;
CREATE TRIGGER strategy_runtime_notify AFTER INSERT ON platform.strategy_runtime_outbox
    FOR EACH ROW EXECUTE FUNCTION platform.strategy_runtime_notify();
CREATE FUNCTION platform.strategy_runtime_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.event_id,NEW.runtime_id,NEW.payload,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.event_id,OLD.runtime_id,OLD.payload,OLD.created_at)
    THEN RAISE EXCEPTION 'immutable strategy runtime wakeup'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER strategy_runtime_immutable BEFORE UPDATE ON platform.strategy_runtime_outbox
    FOR EACH ROW EXECUTE FUNCTION platform.strategy_runtime_immutable();
