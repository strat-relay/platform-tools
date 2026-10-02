from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from core.strategies.evaluation import canonical_bytes

EVENT_ENVELOPE_VERSION = "event-envelope.v1"
NATS_VERSION = "2.10"

SUBJECTS = frozenset({
    "strategy.version.frozen.v1",
    "strategy.promoted.v1",
    "strategy.candidate.detected.v1",
    "strategy.review.completed.v1",
    "signal.entry.created.v1",
    "execution.intent.created.v1",
    "execution.result.recorded.v1",
    "broker.state.updated.v1",
    "ownership.changed.v1",
    # NATS-first real-time data plane (architecture/nats-first-data-plane).
    # Deliberately outside the "strategy."/"signal." prefixes so it is never
    # picked up by TRADING_CORE's prefix-derived subject list below: this
    # subject's durable JetStream acceptance, not a PostgreSQL commit, is the
    # acceptance event, and it must stay on its own stream (SIGNAL_REALTIME)
    # so its ordering/retention/consumer set never entangles with the
    # DB-first outbox-relay-originated `signal.entry.created.v1` reporting
    # event on TRADING_CORE. The dormant SIGNAL_DATA_PLANE_MODE=NATS_FIRST
    # path is the only producer; DB_FIRST (the default) never uses it.
    "realtime.signal.entry.accepted.v1",
    # P4.2 management events are explicit versioned subjects.  They are not wildcarded:
    # stream ownership must remain auditable and non-overlapping.
    "trade.opened.v1",
    "trade.observation.recorded.v1",
    "trade.decision.made.v1",
    # Execution authority state transitions published by authority_store.py; consumed by
    # the realtime API and any subscriber that needs to react to authority changes.
    "system.status_changed",
})

STREAMS = {
    # Core carries strategy, signal, and explicit management lifecycle subjects.  Do not use
    # a trade.> wildcard: TRADING_OBSERVATION owns the observation subject separately.
    "TRADING_CORE": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("strategy.", "signal."))
                                                    or x in {"trade.opened.v1", "trade.decision.made.v1"})),
                     "storage": "file", "retention": "limits", "max_age": 30 * 24 * 60 * 60},
    "EXECUTION": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("execution.", "broker.", "ownership.", "system.")))),
                   "storage": "file", "retention": "limits", "max_age": 30 * 24 * 60 * 60},
    # Real-time hot path only. Retention is deliberately short: durable
    # acceptance for delivery, not long-term audit (PostgreSQL, via the
    # projector, remains the query/reporting history). Sizing is an open
    # decision (see docs/nats_first_data_plane); the value below is a
    # prototype default, not a production recommendation.
    "SIGNAL_REALTIME": {"subjects": ("realtime.signal.entry.accepted.v1",),
                        "storage": "file", "retention": "limits", "max_age": 7 * 24 * 60 * 60},
    # The one P4-owned stream this vertical slice actually needs (A6 08, A7 10). Retention is
    # an explicit SHIP-FIRST value, not a final decision - tracked in
    # docs/engineering/OPTIMIZATION_REGISTER.md ("TRADING_OBSERVATION retention review").
    "TRADING_OBSERVATION": {"subjects": ("trade.observation.recorded.v1",),
                            "storage": "file", "retention": "limits", "max_age": 7 * 24 * 60 * 60},
}


def validate_subject(subject: str) -> str:
    if subject not in SUBJECTS:
        raise ValueError(f"unknown or unversioned subject: {subject}")
    return subject


def _occurred(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("occurred_at requires timezone")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if not str(value).strip():
        raise ValueError("occurred_at is required")
    return str(value)


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int | None
    occurred_at: str | datetime
    payload: Mapping[str, Any]
    correlation_id: str | None = None
    causation_id: str | None = None
    producer: str | None = None
    schema_version: str = EVENT_ENVELOPE_VERSION

    def __post_init__(self) -> None:
        validate_subject(self.event_type)
        if not self.event_id or not self.aggregate_type or not self.aggregate_id:
            raise ValueError("event identity fields are required")
        object.__setattr__(self, "occurred_at", _occurred(self.occurred_at))

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "event_type": self.event_type,
                "schema_version": self.schema_version, "aggregate_type": self.aggregate_type,
                "aggregate_id": self.aggregate_id, "aggregate_version": self.aggregate_version,
                "occurred_at": self.occurred_at, "correlation_id": self.correlation_id,
                "causation_id": self.causation_id, "producer": self.producer,
                "payload": dict(self.payload)}

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())
