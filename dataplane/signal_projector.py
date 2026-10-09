"""SignalPersistenceProjector: JetStream (realtime.signal.entry.accepted.v1) -> PostgreSQL.

    JetStream (at-least-once delivery)
        -> claim_inbox(consumer_name, event_id)   [transport-level idempotency: skip fast]
        -> canonical_signal(payload)               [reused, deterministic - recomputes the
                                                     SAME signal_id/entry_signal_hash the
                                                     publisher derived, from the wire payload
                                                     alone]
        -> ingest_signal(conn, canonical)          [reused unmodified - domain-level
                                                     idempotency: SELECT ... FOR UPDATE by
                                                     signal_id, ON CONFLICT DO NOTHING /
                                                     CanonicalSignalIdentityConflict on a
                                                     genuine content mismatch]
        -> mark_inbox_processed
        -> ACK

Three independent idempotency layers, matching migration/signal.py's own docstring convention
of never claiming exactly-once transport: (1) JetStream Nats-Msg-Id (upstream of this module,
in the publisher/relay), (2) this module's consumer inbox (skip re-processing cost on
redelivery), (3) `ingest_signal`'s own signal_id-keyed uniqueness (the actual domain
correctness guarantee - point 2 is an optimisation, point 3 is the proof). Redelivery after a
crash at any point re-runs the same deterministic sequence and converges to one logical
PostgreSQL EntrySignal (tests/test_dataplane_projector.py).

This module materializes into the SAME relational schema the DB-first path uses
(strategy.candidates, strategy.evaluations/decision_traces/..., strategy.entry_signals,
strategy.entry_signal_mechanisms, strategy.signals) - no JSON-blob shortcut, no parallel
schema. `ingest_signal` additionally writes `platform.outbox_events` rows for
`signal.entry.created.v1`/`strategy.candidate.detected.v1`; those are picked up by the
EXISTING, unmodified `OutboxRelay`, so once the projector commits, downstream consumers of the
DB-first reporting event see the SAME confirmation they would under DB_FIRST mode - the two
modes converge on one persisted fact and one reporting event, never two authorities for the
same signal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from infrastructure.messaging.contracts import EventEnvelope
from migration.signal import CanonicalSignalIdentityConflict, canonical_signal, ingest_signal
from postgres.foundation import claim_inbox, mark_inbox_processed
from execution_v2.trace import emit as trace_emit


@dataclass
class ProjectorMetrics:
    received: int = 0
    processed_new: int = 0
    processed_duplicate_inbox: int = 0
    processed_duplicate_domain: int = 0  # ingest_signal returned False (already present, same content)
    identity_conflicts: int = 0
    failures: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class SignalProjectionConflict(RuntimeError):
    """A redelivered/duplicate event_id carries content that disagrees with an already
    persisted EntrySignal of the same signal_id. This is never silently resolved by
    overwriting; it is surfaced for reconciliation, exactly like the pre-existing
    CanonicalSignalIdentityConflict it wraps."""


class SignalPersistenceProjector:
    def __init__(self, conn_factory: Callable[[], Any], *, consumer_name: str = "signal-persistence-projector",
                 on_begin: Callable[[], None] | None = None, on_commit: Callable[[], None] | None = None):
        """``conn_factory`` returns a fresh connection per event (mirrors how a real
        deployment would use a pool); tests/benchmark pass a factory over a fake in-memory
        connection. ``on_begin``/``on_commit`` are latency-instrumentation hooks (T5/T6)."""
        self.conn_factory = conn_factory
        self.consumer_name = consumer_name
        self.metrics = ProjectorMetrics()
        self.on_begin = on_begin
        self.on_commit = on_commit

    async def handle(self, envelope: EventEnvelope) -> bool:
        """Returns True if this delivery caused a NEW logical PostgreSQL EntrySignal to be
        created; False for every duplicate/no-op outcome. Raises SignalProjectionConflict for
        a genuine content mismatch (never silently overwritten)."""
        self.metrics.received += 1
        payload = envelope.payload
        trace_emit("SIGNAL_PROJECTOR_RECEIVED", signal_id=envelope.aggregate_id,
                   event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                   signal_emitted_at=payload.get("signal_emitted_at"), created_at=payload.get("created_at"),
                   transport="NATS_FIRST", projector=self.consumer_name)
        conn = self.conn_factory()
        try:
            if not claim_inbox(conn, self.consumer_name, envelope.event_id):
                conn.commit()
                self.metrics.processed_duplicate_inbox += 1
                trace_emit("SIGNAL_PROJECTOR_DUPLICATE_INBOX", signal_id=envelope.aggregate_id,
                           event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                           signal_emitted_at=payload.get("signal_emitted_at"), outcome="DUPLICATE")
                return False
            if self.on_begin is not None:
                self.on_begin()
            trace_emit("SIGNAL_PROJECTOR_DB_BEGIN", signal_id=envelope.aggregate_id,
                       event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                       signal_emitted_at=payload.get("signal_emitted_at"), outcome="CLAIMED")
            canonical = canonical_signal(envelope.payload, runtime_version="signal-orchestrator.v1",
                                         evaluator_version="nats-first-signal-publisher.v1",
                                         stage_id="orchestrator_acceptance", primitive_id="orchestrator.strategy_signal")
            try:
                inserted = ingest_signal(conn, canonical, occurred_at=envelope.occurred_at)
            except CanonicalSignalIdentityConflict as exc:
                self.metrics.identity_conflicts += 1
                raise SignalProjectionConflict(str(exc)) from exc
            mark_inbox_processed(conn, self.consumer_name, envelope.event_id)
            conn.commit()
            if self.on_commit is not None:
                self.on_commit()
            trace_emit("SIGNAL_PROJECTOR_DB_COMMITTED", signal_id=envelope.aggregate_id,
                       event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                       signal_emitted_at=payload.get("signal_emitted_at"), outcome="INSERTED" if inserted else "DUPLICATE")
            if inserted:
                self.metrics.processed_new += 1
            else:
                self.metrics.processed_duplicate_domain += 1
            return inserted
        except SignalProjectionConflict:
            conn.rollback()
            trace_emit("SIGNAL_PROJECTOR_FAILED", signal_id=envelope.aggregate_id,
                       event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                       signal_emitted_at=payload.get("signal_emitted_at"), outcome="CONFLICT", error="identity conflict")
            raise
        except Exception:
            conn.rollback()
            self.metrics.failures += 1
            trace_emit("SIGNAL_PROJECTOR_FAILED", signal_id=envelope.aggregate_id,
                       event_id=envelope.event_id, decision_time=payload.get("decision_time"),
                       signal_emitted_at=payload.get("signal_emitted_at"), outcome="FAILED")
            raise

    async def run_forever(self, consume: Callable[[Callable[[EventEnvelope], Awaitable[bool]]], Awaitable[Any]]) -> Any:
        """``consume`` is an injected subscribe-and-ack loop (e.g. JetStreamConsumer.consume
        bound to REALTIME_SIGNAL_SUBJECT); this method never opens a subscription itself so
        tests can drive `handle()` directly against a deterministic message sequence."""
        return await consume(self.handle)
