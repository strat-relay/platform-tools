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
})

STREAMS = {
    "TRADING_CORE": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("strategy.", "signal.")))),
                     "storage": "file", "retention": "limits", "max_age": "30d"},
    "EXECUTION": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("execution.", "broker.", "ownership.")))),
                   "storage": "file", "retention": "limits", "max_age": "30d"},
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
