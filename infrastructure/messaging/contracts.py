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
    # P4.2 (architecture/p4-2-managed-trade). trade.opened.v1/trade.decision.made.v1 are
    # explicit additions to TRADING_CORE (never a `trade.>` wildcard, A7 10);
    # trade.observation.recorded.v1 is high-frequency and gets its own stream below so its
    # retention/ordering never entangles with TRADING_CORE's low-frequency domain events.
    "trade.opened.v1",
    "trade.observation.recorded.v1",
    "trade.decision.made.v1",
})

# Explicit membership for the trade.* subjects that belong on TRADING_CORE (never a `trade.>`
# wildcard - A7 10 "explicit trade/management subjects (no trade.>)").
_TRADING_CORE_TRADE_SUBJECTS = ("trade.opened.v1", "trade.decision.made.v1")

STREAMS = {
    "TRADING_CORE": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("strategy.", "signal."))
                                              or x in _TRADING_CORE_TRADE_SUBJECTS)),
                     "storage": "file", "retention": "limits", "max_age": 30 * 24 * 60 * 60},
    "EXECUTION": {"subjects": tuple(sorted(x for x in SUBJECTS if x.startswith(("execution.", "broker.", "ownership.")))),
                   "storage": "file", "retention": "limits", "max_age": 30 * 24 * 60 * 60},
    # Real-time trade-observation hot path only (A6 08, A7 10). Retention is an open decision
    # (OD-08: "expose as configuration with no production default committed"); the value below
    # is a prototype placeholder, not a committed production number - see
    # docs/p4_2_managed_trade/README.md.
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
