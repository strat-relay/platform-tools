"""Durable immutable evidence and notification outbox for the opt-in library stream."""
from contextlib import contextmanager
import json
import math
import os
import re

from core.strategies.evaluation import canonical_hash
from postgres.db import transaction

MARKET = 'market.evidence.committed.v1'
ENTRY = 'research.library.entry.observed.v1'
SCHEMA = 'library-event-stream.v1'


def routed_subject(subject, payload):
    if subject == MARKET:
        # UTF-8 hex is a reversible single NATS token even for dotted broker symbols.
        token=payload['provider_symbol'].encode('utf-8').hex()
        if not token or len(token)>256:raise ValueError('invalid market route symbol')
    elif subject == ENTRY:
        token=payload['run_id']
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',token):raise ValueError('invalid research run route')
    else:raise ValueError('unknown event transport subject')
    return subject+'.'+token


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def open_connection(*, autocommit=False):
    import psycopg
    # Dedicated, explicitly provisioned connection; never borrow broker/admin credentials.
    dsn = os.environ.get('LIBRARY_EVENT_DSN')
    if not dsn:
        raise ValueError('LIBRARY_EVENT_DSN is required')
    conn = psycopg.connect(dsn, autocommit=autocommit)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(),pg_is_in_recovery(),to_regclass('research.library_stream_outbox')")
            database, recovery, table = cur.fetchone()
        if database != os.environ.get('LIBRARY_EVENT_DATABASE', 'trading') or recovery or table is None:
            raise ValueError('event transport requires verified primary database and migration 070')
        if not autocommit:
            conn.rollback()
        return conn
    except Exception:
        conn.close()
        raise


class EventStore:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def atomic(self, consumer):
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (consumer,))
            yield

    def event(self, event_id, subject):
        with self.conn.cursor() as cur:
            cur.execute('SELECT subject,payload FROM research.library_stream_outbox WHERE event_id=%s', (event_id,))
            row = cur.fetchone()
        if not row or row[0] != subject or row[1].get('schema') != SCHEMA:
            raise ValueError('unknown or mismatched durable event')
        payload = row[1]
        if canonical_hash(payload) != event_id:
            raise ValueError('durable event identity conflict')
        return payload

    def seen(self, consumer, event_id):
        with self.conn.cursor() as cur:
            cur.execute('SELECT result FROM research.library_stream_inbox WHERE consumer_id=%s AND event_id=%s',
                        (consumer,event_id))
            return cur.fetchone() is not None

    def done(self, consumer, event_id, result):
        with self.conn.cursor() as cur:
            cur.execute('INSERT INTO research.library_stream_inbox(consumer_id,event_id,result) VALUES (%s,%s,%s)',
                        (consumer,event_id,result))

    def append(self, subject, provider, payload, sequence=None):
        event_id = canonical_hash(payload)
        with self.conn.cursor() as cur:
            cur.execute('''INSERT INTO research.library_stream_outbox(event_id,subject,provider_symbol,sequence,payload)
                VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(event_id) DO NOTHING''',
                (event_id,subject,provider,sequence,encode(payload)))
        return event_id

    def market(self, provider, observed_at, *, snapshot=None, quote=None):
        if not provider or not math.isfinite(observed_at) or observed_at <= 0:
            raise ValueError('market identity/time required')
        with self.atomic('market-source:'+provider):
            with self.conn.cursor() as cur:
                cur.execute('SELECT snapshot_id,sequence,observed_at FROM research.market_event_head WHERE provider_symbol=%s', (provider,))
                head = cur.fetchone()
                if head and observed_at < head[2]:
                    raise ValueError('market source clock moved backwards')
                if snapshot is None and head is None:
                    return None  # quotes cannot invent a historical snapshot
                snapshot_id = head[0] if snapshot is None else canonical_hash(snapshot)
                if snapshot is not None:
                    if snapshot.get('provider_symbol') != provider or snapshot.get('observed_at') != observed_at:
                        raise ValueError('market snapshot identity mismatch')
                    cur.execute('''INSERT INTO research.market_event_snapshot(snapshot_id,provider_symbol,payload)
                        VALUES (%s,%s,%s::jsonb) ON CONFLICT(snapshot_id) DO NOTHING''',
                        (snapshot_id,provider,encode(snapshot)))
                sequence = head[1]+1 if head else 1
                cur.execute('''INSERT INTO research.market_event_head(provider_symbol,snapshot_id,sequence,observed_at)
                    VALUES (%s,%s,%s,%s) ON CONFLICT(provider_symbol) DO UPDATE SET
                    snapshot_id=EXCLUDED.snapshot_id,sequence=EXCLUDED.sequence,observed_at=EXCLUDED.observed_at''',
                    (provider,snapshot_id,sequence,observed_at))
            return self.append(MARKET,provider,{'schema':SCHEMA,'provider_symbol':provider,
                'sequence':sequence,'snapshot_id':snapshot_id,'observed_at':observed_at,'quote':quote,
                'kind':'SNAPSHOT' if snapshot is not None else 'QUOTE'},sequence)

    def snapshot(self, snapshot_id, provider):
        with self.conn.cursor() as cur:
            cur.execute('SELECT payload FROM research.market_event_snapshot WHERE snapshot_id=%s AND provider_symbol=%s',
                        (snapshot_id,provider))
            row = cur.fetchone()
        if not row or canonical_hash(row[0]) != snapshot_id:
            raise ValueError('immutable market snapshot missing or changed')
        return row[0]

    def cursor(self, consumer, provider):
        with self.conn.cursor() as cur:
            cur.execute('SELECT sequence FROM research.library_stream_cursor WHERE consumer_id=%s AND provider_symbol=%s',
                        (consumer,provider))
            row = cur.fetchone()
            return row[0] if row else 0

    def advance_cursor(self, consumer, provider, sequence):
        with self.conn.cursor() as cur:
            cur.execute('''INSERT INTO research.library_stream_cursor(consumer_id,provider_symbol,sequence)
                VALUES (%s,%s,%s) ON CONFLICT(consumer_id,provider_symbol) DO UPDATE SET sequence=EXCLUDED.sequence''',
                (consumer,provider,sequence))

    def pending(self, limit=100):
        # One unpublished market event per symbol at a time preserves causal source order
        # even across relay replicas. A failed event blocks later events for that symbol.
        with self.conn.cursor() as cur:
            cur.execute('''SELECT o.event_id,o.subject FROM research.library_stream_outbox o
                WHERE o.published_at IS NULL AND NOT EXISTS (
                    SELECT 1 FROM research.library_stream_outbox earlier
                    WHERE earlier.subject=o.subject AND earlier.provider_symbol=o.provider_symbol
                      AND earlier.published_at IS NULL AND earlier.sequence<o.sequence)
                ORDER BY o.created_at,o.event_id LIMIT %s FOR UPDATE OF o SKIP LOCKED''', (limit,))
            return cur.fetchall()


class CollectorEventSink:
    def snapshot(self, provider, snapshot, bars, observed_at):
        from market_data_cache.store import unexpected_missing_times
        if any(unexpected_missing_times(rows,tf) for tf,rows in bars.items()):
            raise ValueError('cannot publish discontinuous market evidence')
        payload = {'provider_symbol':provider,'observed_at':observed_at,
            'snapshot':snapshot,'bars':bars}
        with open_connection() as conn:
            return EventStore(conn).market(provider,observed_at,snapshot=payload,
                quote={'quote':snapshot['quote'],'observed_at':observed_at})

    def quote(self, provider, quote, observed_at):
        with open_connection() as conn:
            return EventStore(conn).market(provider,observed_at,
                quote={'quote':quote,'observed_at':observed_at})


def collector_sink_from_env():
    mode = os.environ.get('MARKET_DATA_EVENT_MODE','OFF')
    if mode == 'OFF':
        return None
    if mode != 'JETSTREAM_OUTBOX':
        raise ValueError('unsupported MARKET_DATA_EVENT_MODE')
    with open_connection():
        pass
    return CollectorEventSink()
