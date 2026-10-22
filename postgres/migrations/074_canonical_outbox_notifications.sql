-- The final canonical publication hop must also wake on commit, not a timer.
-- Transactional NOTIFY is delivered only after the entry/outbox transaction commits.
CREATE FUNCTION platform.canonical_outbox_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('canonical_outbox_events', '');
    RETURN NEW;
END;
$$;
CREATE TRIGGER canonical_outbox_notify AFTER INSERT ON platform.outbox_events
    FOR EACH ROW EXECUTE FUNCTION platform.canonical_outbox_notify();
