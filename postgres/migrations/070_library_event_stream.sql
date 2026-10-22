-- Opt-in event transport only. No registrations, grants, authority or workload changes.
CREATE TABLE research.market_event_snapshot (
    snapshot_id text PRIMARY KEY,
    provider_symbol text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE research.market_event_head (
    provider_symbol text PRIMARY KEY,
    snapshot_id text NOT NULL REFERENCES research.market_event_snapshot(snapshot_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    observed_at double precision NOT NULL
);
CREATE TABLE research.library_stream_outbox (
    event_id text PRIMARY KEY,
    subject text NOT NULL CHECK (subject IN ('market.evidence.committed.v1', 'research.library.entry.observed.v1')),
    provider_symbol text NOT NULL,
    sequence bigint,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    UNIQUE(subject, provider_symbol, sequence)
);
CREATE INDEX library_stream_pending_idx ON research.library_stream_outbox(created_at,event_id)
    WHERE published_at IS NULL;
CREATE TABLE research.library_stream_cursor (
    consumer_id text NOT NULL,
    provider_symbol text NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    PRIMARY KEY (consumer_id,provider_symbol)
);
CREATE TABLE research.library_stream_inbox (
    consumer_id text NOT NULL,
    event_id text NOT NULL REFERENCES research.library_stream_outbox(event_id),
    result text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer_id,event_id)
);
CREATE FUNCTION research.library_stream_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('library_stream_outbox', '');
    RETURN NEW;
END;
$$;
CREATE TRIGGER library_stream_outbox_notify AFTER INSERT ON research.library_stream_outbox
    FOR EACH ROW EXECUTE FUNCTION research.library_stream_notify();
-- Immutable evidence/outbox content; only relay publication bookkeeping may change.
CREATE FUNCTION research.library_stream_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'market_event_snapshot' THEN
        RAISE EXCEPTION 'immutable market snapshot';
    END IF;
    IF ROW(NEW.event_id,NEW.subject,NEW.provider_symbol,NEW.sequence,NEW.payload,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.event_id,OLD.subject,OLD.provider_symbol,OLD.sequence,OLD.payload,OLD.created_at)
    THEN RAISE EXCEPTION 'immutable library stream evidence'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER market_event_snapshot_immutable BEFORE UPDATE ON research.market_event_snapshot
    FOR EACH ROW EXECUTE FUNCTION research.library_stream_immutable();
CREATE TRIGGER library_stream_outbox_immutable BEFORE UPDATE ON research.library_stream_outbox
    FOR EACH ROW EXECUTE FUNCTION research.library_stream_immutable();
