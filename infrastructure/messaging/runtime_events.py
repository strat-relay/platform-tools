"""Shared event wakeups for existing strategy evaluators and the signal authority.

Messages are scheduling hints, never market evidence or execution authority. Each
cycle reads current canonical inputs and existing durable strategy state. A missed
hint is healed by startup/periodic reconciliation; no candle is backdated to a hint.
The immutable library event consumer remains available for evidence-bound replay.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager

READY = 'runtime.strategy.evidence.ready.v1'
STREAM = 'STRATEGY_RUNTIME'
CACHE = 'runtime.market.cache.ready.v1'


def mode():
    value = os.environ.get('STRATEGY_EVENT_MODE', 'POLL')
    if value not in {'POLL', 'JETSTREAM'}:
        raise ValueError('STRATEGY_EVENT_MODE must be POLL or JETSTREAM')
    return value


def identity():
    value = os.environ.get('STRATEGY_EVENT_RUNTIME_ID', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value):
        raise ValueError('explicit stable STRATEGY_EVENT_RUNTIME_ID required')
    return value


def report(event, **fields):
    print(json.dumps({'event': event, 'observed_at': time.time(), **fields}, allow_nan=False), flush=True)


@contextmanager
def connection():
    import psycopg
    dsn = os.environ.get('STRATEGY_EVENT_DSN')
    database = os.environ.get('STRATEGY_EVENT_DATABASE')
    if not dsn or not database:
        raise ValueError('dedicated STRATEGY_EVENT_DSN and explicit STRATEGY_EVENT_DATABASE required')
    with psycopg.connect(dsn, connect_timeout=3,
                         options='-c statement_timeout=2000 -c lock_timeout=500') as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(),pg_is_in_recovery(),to_regclass('platform.strategy_runtime_outbox')")
            actual, recovery, table = cur.fetchone()
        if actual != database or recovery or table is None:
            raise ValueError('strategy events require verified primary and migration 072')
        conn.rollback()
        yield conn


def notify_ready(revision, *, conn=None, provider_symbol=None):
    """Call only after file checkpoint persistence, or inside the evidence DB transaction.

    Same revision retries have the same ID. If file persistence succeeds and this
    enqueue fails, startup/reconciliation rediscovers the durable entry; callers
    must report the failure rather than discarding/resetting the checkpoint.
    """
    if mode() != 'JETSTREAM':
        return None
    runtime = identity()
    revision_hash = hashlib.sha256(json.dumps(revision, sort_keys=True, separators=(',', ':'),
                                             allow_nan=False, default=str).encode()).hexdigest()
    payload = {'schema': 'strategy-runtime-wakeup.v1', 'runtime_id': runtime, 'revision': revision_hash,
               'provider_symbol': provider_symbol}
    event_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def insert(db):
        with db.cursor() as cur:
            cur.execute('''INSERT INTO platform.strategy_runtime_outbox(event_id,runtime_id,payload)
                VALUES (%s,%s,%s::jsonb) ON CONFLICT(event_id) DO NOTHING''',
                (event_id, runtime, json.dumps(payload)))
    if conn is not None:
        insert(conn)
    else:
        with connection() as db:
            insert(db)
    report('strategy_evidence_ready_committed' if conn is None else 'strategy_evidence_ready_enqueued',
           runtime_id=runtime, event_id=event_id)
    return event_id


class Wakeup:
    """One serial business loop; NATS reader only signals a condition variable.

    Generation counters retain events arriving DURING evaluation or before wait().
    No unbounded queue, no overlapping cycles, no cross-thread evaluator access.
    ACK means a wakeup was accepted, NOT that an evaluation/fill was committed.
    """
    def __init__(self, *, orchestrator=False, symbols=None):
        self.enabled = mode() == 'JETSTREAM'
        self.orchestrator = orchestrator
        self.symbols = tuple(sorted(set(symbols or ())))
        self.condition = threading.Condition()
        self.generation = self.consumed = 0
        self.healthy = False
        self.stopped = threading.Event()
        self.thread = None
        self.error = None
        self.last_event_id = None
        self.last_received_at = None
        self.last_received_monotonic = None
        if self.enabled:
            self.runtime = identity()
            self.reconcile = float(os.environ.get('STRATEGY_EVENT_RECONCILE_SECONDS', '30'))
            if not math.isfinite(self.reconcile) or not 1 <= self.reconcile <= 60:
                raise ValueError('reconciliation interval must be in [1,60] seconds')
            if os.environ.get('MARKET_DATA_SOURCE', '').upper() != 'REDIS':
                raise ValueError('event-driven strategies require canonical REDIS inputs')
            if not os.environ.get('NATS_URL') or not os.environ.get('P2_NATS_USER') or not os.environ.get('P2_NATS_PASSWORD'):
                raise ValueError('explicit scoped NATS connection required')

    def receive(self, event_id, received_at):
        with self.condition:
            self.generation += 1
            self.last_event_id, self.last_received_at = event_id, received_at
            self.last_received_monotonic = time.monotonic()
            self.condition.notify_all()

    def start(self):
        if not self.enabled or self.thread is not None:
            return self
        self.thread = threading.Thread(target=self._thread, name='strategy-nats-wakeup', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        with self.condition:
            while not self.healthy and self.error is None and time.monotonic() < deadline:
                self.condition.wait(max(0, deadline-time.monotonic()))
        try:
            self.check()
        except Exception:
            self.close()
            raise
        return self

    def check(self):
        if self.enabled and (not self.healthy or self.error is not None):
            raise RuntimeError('strategy event subscription unavailable; no silent polling fallback')

    def wait(self, poll_seconds, *, stopping=lambda: False):
        if not self.enabled:
            time.sleep(max(0, poll_seconds))
            return 'POLL'
        self.start()
        deadline = time.monotonic() + self.reconcile
        with self.condition:
            while self.generation == self.consumed and not self.stopped.is_set() and not stopping():
                self.check()
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(remaining, 0.5))
            self.check()
            reason = 'EVENT' if self.generation != self.consumed else 'RECONCILIATION'
            self.consumed = self.generation
        report('strategy_cycle_woken', runtime_id=self.runtime, reason=reason,
               event_id=self.last_event_id, event_received_at=self.last_received_at,
               dispatch_ms=((time.monotonic()-self.last_received_monotonic)*1000
                            if self.last_received_monotonic is not None else None),
               generation=self.consumed)
        return reason

    def close(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _thread(self):
        try:
            asyncio.run(self._listen())
        except Exception as exc:
            # Never log connection strings or authentication errors verbatim.
            with self.condition:
                self.error = type(exc).__name__
                self.healthy = False
                self.condition.notify_all()
            report('strategy_event_subscription_failed', runtime_id=self.runtime, error_type=self.error)

    async def _listen(self):
        import nats
        from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
        from nats.errors import TimeoutError as NatsTimeoutError

        async def disconnected():
            with self.condition:
                self.healthy = False
                self.condition.notify_all()

        async def reconnected():
            with self.condition:
                self.healthy = True
                self.condition.notify_all()

        async def error_callback(exc):
            report('strategy_nats_error', runtime_id=self.runtime, error_type=type(exc).__name__)

        nc = await nats.connect(os.environ['NATS_URL'], user=os.environ['P2_NATS_USER'],
            password=os.environ['P2_NATS_PASSWORD'], name='strategy-'+self.runtime,
            connect_timeout=5, max_reconnect_attempts=2, error_cb=error_callback,
            disconnected_cb=disconnected, reconnected_cb=reconnected)
        try:
            js = nc.jetstream()
            filters = ([CACHE+'.'+s.encode().hex() for s in self.symbols]
                       if self.symbols else [CACHE+'.*'])
            if self.orchestrator:
                filters.append(READY+'.*')
            routes = [(STREAM, filters)]
            subscriptions = []
            for stream, filters in routes:
                info = await js.stream_info(stream)
                if (set(info.config.subjects) != {CACHE+'.*', READY+'.*'}
                        or info.config.storage != 'file' or not 0 < info.config.max_age <= 86400
                        or not 0 < info.config.max_bytes <= 64*1024*1024
                        or info.config.discard != 'new' or info.config.max_msg_size != 1024):
                    raise ValueError('strategy wakeup stream ownership/storage mismatch')
                durable = 'wakeup-'+self.runtime+'-'+stream.lower()
                sub = await js.pull_subscribe(filters[0], durable=durable, stream=stream,
                    config=ConsumerConfig(durable_name=durable, filter_subjects=filters,
                        ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.NEW,
                        ack_wait=30, max_ack_pending=64))
                actual = (await js.consumer_info(stream, durable)).config
                actual_filters = set(actual.filter_subjects or [actual.filter_subject])
                if (actual_filters != set(filters) or actual.ack_policy != AckPolicy.EXPLICIT
                        or actual.deliver_policy != DeliverPolicy.NEW or actual.max_ack_pending != 64):
                    raise ValueError('existing wakeup consumer configuration conflict')
                subscriptions.append(sub)
            await reconnected()

            async def consume(sub):
                while not self.stopped.is_set():
                    try:
                        # A large batch fetch may wait for its batch/timeout even
                        # after the first message arrives. One message is the wakeup.
                        messages = await sub.fetch(1, timeout=1)
                    except NatsTimeoutError:
                        continue
                    for message in messages:
                        if len(message.data) > 1024:
                            raise ValueError('oversized strategy wakeup')
                        payload = json.loads(message.data)
                        if set(payload) != {'event_id'} or not re.fullmatch('[0-9a-f]{64}', payload['event_id']):
                            raise ValueError('invalid strategy wakeup reference')
                        self.receive(payload['event_id'], time.time())
                        await message.ack_sync()
            tasks = [asyncio.create_task(consume(sub)) for sub in subscriptions]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await nc.close()


def notify_cache_ready(provider_symbol, revision):
    """After Redis is visible. Transport failure degrades event health, never the shared feed.

    Existing periodic reconciliation also reads cache, so even a collector crash
    between Redis and this enqueue cannot permanently hide a completed candle.
    """
    if mode() != 'JETSTREAM':
        return
    global _cache_notifier
    with _cache_notifier_lock:
        if _cache_notifier is None:
            _cache_notifier = CacheNotifier()
        notifier = _cache_notifier
    notifier.submit(provider_symbol, revision)


class CacheNotifier:
    """Coalesce cache hints off the shared collector thread, with bounded memory.

    These are latest-cache hints, not the immutable market event ledger. Replacing
    an older unsent hint with a newer one is safe; readers always load current cache.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = {}
        self.stopped = False
        self.last_success_at = None
        self.last_error_type = None
        self.thread = threading.Thread(target=self._run, name='cache-ready-outbox', daemon=True)
        self.thread.start()

    def submit(self, provider, revision):
        with self.condition:
            if provider not in self.pending and len(self.pending) >= 256:
                report('strategy_cache_wakeup_overflow', provider_symbol=provider)
                return  # bounded reconciliation heals the lost scheduling hint
            self.pending[provider] = revision
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                while not self.pending and not self.stopped:
                    self.condition.wait()
                if self.stopped:
                    return
                provider = next(iter(self.pending))
                revision = self.pending.pop(provider)
            try:
                notify_ready(revision, provider_symbol=provider)
                with self.condition:
                    self.last_success_at = time.time()
                    self.last_error_type = None
            except Exception as exc:
                with self.condition:
                    self.last_error_type = type(exc).__name__
                report('strategy_cache_wakeup_failed', provider_symbol=provider,
                       error_type=type(exc).__name__)
                with self.condition:
                    if provider in self.pending or len(self.pending) < 256:
                        self.pending.setdefault(provider, revision)
                time.sleep(2)  # transport retry only, outside collector/evaluator threads

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        self.thread.join(timeout=3)


_cache_notifier_lock = threading.Lock()
_cache_notifier = None


def cache_event_health():
    if mode() != 'JETSTREAM':
        return {'status': 'DISABLED'}
    notifier = _cache_notifier
    if notifier is None:
        return {'status': 'STARTING'}
    with notifier.condition:
        return {'status': 'DEGRADED' if notifier.last_error_type else
                          ('RUNNING' if notifier.last_success_at else 'STARTING'),
                'last_success_at': notifier.last_success_at,
                'last_error_type': notifier.last_error_type, 'pending_symbols': len(notifier.pending)}
