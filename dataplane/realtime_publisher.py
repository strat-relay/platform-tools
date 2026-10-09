"""Direct-to-JetStream signal acceptance (NATS-first hot path).

    accepted StrategySignal -> canonicalize (migration.signal.canonical_signal, reused
    unmodified) -> deterministic signal_id (already assigned upstream; never derived here)
    -> EventEnvelope -> JetStreamPublisher.publish() -> PUBACK required -> REALTIME_SIGNAL_ACCEPTED

PostgreSQL is not imported, opened, or required anywhere in this module. The payload carries
the FULL validated raw signal record (not a small reference), because JetStream - not
PostgreSQL - is the durable store at the instant of acceptance: there is nothing in PostgreSQL
yet for a reference to point at. `SignalPersistenceProjector` (signal_projector.py) recovers
the complete EntrySignal by calling the SAME `canonical_signal()` on that payload, which is why
recomputing it is required to be deterministic (proven in tests/test_dataplane_realtime_publisher.py
and reused from the existing `migration.signal` test suite's own stability guarantees).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from migration.signal import CanonicalSignal, canonical_signal
from execution_v2.trace import emit as trace_emit

REALTIME_SIGNAL_SUBJECT = "realtime.signal.entry.accepted.v1"


class RealtimePublicationFailed(RuntimeError):
    """JetStream did not durably acknowledge the publish.

    The signal is NOT reported as accepted. The caller decides retry policy; this class never
    retries silently and never falls back to a different transport. Because the underlying
    event_id is deterministic (derived from the canonical signal_id), a caller-initiated retry
    with the identical CanonicalSignal is always safe to attempt again: at worst it is a
    duplicate publish, which JetStream's own Nats-Msg-Id dedupe window and the projector's
    signal_id/entry_signal_hash idempotency (migration.signal.ingest_signal) absorb.
    """

    def __init__(self, signal_id: str, *, cause: Exception):
        self.signal_id = signal_id
        self.cause = cause
        super().__init__(f"REALTIME_SIGNAL_PUBLICATION_FAILED signal_id={signal_id}: {cause}")


@dataclass(frozen=True)
class RealtimePublishResult:
    signal_id: str
    entry_signal_hash: str
    evaluation_hash: str
    event_id: str
    status: str  # always "REALTIME_SIGNAL_ACCEPTED" on success; failure raises instead
    ack: Any


def _event_id(signal_id: str) -> str:
    # Deliberately distinct from the DB-first outbox event id
    # ("<signal_id>:entry.created") so the two are never mistaken for the same
    # logical acceptance event, even though both ultimately describe one signal.
    return f"{signal_id}:entry.accepted"


class RealtimeSignalPublisher:
    """Orchestrator-side NATS-first publisher. No PostgreSQL dependency."""

    def __init__(self, publisher: JetStreamPublisher, *, producer: str = "nats-first-signal-publisher.v1"):
        self.publisher = publisher
        self.producer = producer

    def canonicalize(self, raw: Mapping[str, Any]) -> CanonicalSignal:
        """Validate/canonicalize without publishing; callers may inspect
        signal_id/entry_signal_hash (e.g. to record T1) before the publish call."""
        return canonical_signal(raw, runtime_version="signal-orchestrator.v1",
                                evaluator_version="nats-first-signal-publisher.v1",
                                stage_id="orchestrator_acceptance", primitive_id="orchestrator.strategy_signal")

    async def publish(self, raw: Mapping[str, Any], *, canonical: CanonicalSignal | None = None,
                      on_publish_initiated: Any = None) -> RealtimePublishResult:
        """Publish the FULL raw record. Raises RealtimePublicationFailed on any transport
        failure (including a simulated PUBACK timeout in tests); never returns a
        REALTIME_SIGNAL_ACCEPTED result unless the publish call itself succeeded."""
        canonical = canonical or self.canonicalize(raw)
        occurred_at = raw.get("signal_emitted_at") or canonical.fields["decision_time"]
        envelope = EventEnvelope(
            event_id=_event_id(canonical.signal_id), event_type=REALTIME_SIGNAL_SUBJECT,
            aggregate_type="signal", aggregate_id=canonical.signal_id, aggregate_version=1,
            occurred_at=occurred_at, payload=dict(raw), correlation_id=canonical.signal_id,
            causation_id=None, producer=self.producer,
        )
        trace_emit("SIGNAL_NATS_PUBLISH_STARTED", signal_id=canonical.signal_id,
                   event_id=envelope.event_id, decision_time=canonical.fields.get("decision_time"),
                   signal_emitted_at=raw.get("signal_emitted_at"), created_at=raw.get("created_at"),
                   strategy_id=canonical.fields.get("strategy_id"), transport="NATS_FIRST")
        if on_publish_initiated is not None:
            on_publish_initiated()
        try:
            ack = await self.publisher.publish(envelope)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any transport failure is REALTIME_SIGNAL_PUBLICATION_FAILED
            trace_emit("SIGNAL_NATS_PUBLISH_FAILED", signal_id=canonical.signal_id,
                       event_id=envelope.event_id, decision_time=canonical.fields.get("decision_time"),
                       signal_emitted_at=raw.get("signal_emitted_at"), created_at=raw.get("created_at"),
                       transport="NATS_FIRST", outcome="FAILED", error=f"{type(exc).__name__}: {exc}")
            raise RealtimePublicationFailed(canonical.signal_id, cause=exc) from exc
        trace_emit("SIGNAL_NATS_PUBLISHED", signal_id=canonical.signal_id,
                   event_id=envelope.event_id, decision_time=canonical.fields.get("decision_time"),
                   signal_emitted_at=raw.get("signal_emitted_at"), created_at=raw.get("created_at"),
                   strategy_id=canonical.fields.get("strategy_id"), transport="NATS_FIRST", outcome="PUBACK")
        return RealtimePublishResult(
            signal_id=canonical.signal_id, entry_signal_hash=canonical.entry_signal_hash,
            evaluation_hash=canonical.evaluation.evaluation_hash, event_id=envelope.event_id,
            status="REALTIME_SIGNAL_ACCEPTED", ack=ack,
        )
