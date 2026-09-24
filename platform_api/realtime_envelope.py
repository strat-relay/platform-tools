"""The Console realtime wire contract (mission `CLAUDE-V1.3-CONSOLE-REALTIME-WEBSOCKET-
ARCHITECTURE` section 4). This is the ONLY shape ever sent to a browser over the realtime
WebSocket - never a raw database row, never a raw NATS/JetStream payload. Keeping this module
free of any NATS/PostgreSQL import is deliberate: it documents that the envelope is a UI
transport concern, decoupled from how (or whether) a canonical domain event happens to exist for
a given change (see `realtime_sources.py`'s module docstring, section 14 "keep domain events
separate from UI transport events").

Schema `console-realtime.v1`:

    {
      "schema": "console-realtime.v1",
      "eventId": "<stable, dedup-safe string>",
      "type": "<resource>.<verb>",           # e.g. "signal.created", "trade_observation.created"
      "occurredAt": "<ISO-8601 UTC>",         # when the underlying change actually happened
      "resource": "<subscription channel>",  # "signals" | "trade-management" | "system"
      "resourceId": "<id of the affected row>",
      "sequence": <int>,                     # monotonic per-resource, assigned by RealtimeHub;
                                              # used for gap detection and bounded resume, NOT a
                                              # substitute for eventId-based dedup
      "payload": {...}                       # typed, minimal, resource-specific
    }

`eventId` is stable across redelivery: for a real NATS-sourced event it is derived from the
source event's own `event_id` (already globally unique and stable by construction); for a
bounded-refresh-sourced event (no canonical NATS event exists - see `realtime_sources.py`) it is
derived deterministically from `(resource_id, type, occurred_at)` so the SAME underlying change
never mints two different eventIds even if the poller happens to observe it twice across ticks.
Never a fresh UUID per delivery - that would defeat client-side dedup entirely.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "console-realtime.v1"

RESOURCE_SIGNALS = "signals"
RESOURCE_TRADE_MANAGEMENT = "trade-management"
RESOURCE_SYSTEM = "system"
RESOURCES = (RESOURCE_SIGNALS, RESOURCE_TRADE_MANAGEMENT, RESOURCE_SYSTEM)

# Every event `type` this service will ever emit. Documented exhaustively (mission section 4
# "Document supported event types") so a client can build an exhaustive switch/reducer rather
# than guessing at undocumented shapes.
EVENT_TYPES = (
    "signal.created",              # a new canonical EntrySignal (real NATS event: signal.entry.created.v1)
    "signal.outcome_changed",      # entry_signals.terminal_state transitioned (bounded refresh - no canonical event exists)
    "execution.intent.created.v1", # a durable V2 evaluation was recorded for a signal
    "execution.result.recorded.v1", # a durable V2 execution result was recorded
    "execution.evaluation.updated", # supplemental evaluation state changed
    "managed_trade.created",       # a new OPEN ManagedTrade appeared (bounded refresh - no canonical event exists)
    "trade_observation.created",   # real NATS event: trade.observation.recorded.v1
    "trade_manager_decision.created",   # bounded refresh - no canonical event exists
    "publication_decision.created",     # bounded refresh - no canonical event exists
    "system.status_changed",       # bounded refresh over /api/v1/system's own components
)


@dataclass(frozen=True)
class RealtimeEvent:
    """What a source (`realtime_sources.py`) hands to `RealtimeHub.publish()`. `sequence` is
    deliberately NOT set here - the hub assigns it atomically at publish time, per resource, so
    two concurrent sources can never race on sequence assignment."""

    type: str
    occurred_at: str
    resource: str
    resource_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None  # when the source has a real, stable domain event id, pass it

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown realtime event type: {self.type!r}")
        if self.resource not in RESOURCES:
            raise ValueError(f"unknown realtime resource: {self.resource!r}")

    def stable_event_id(self) -> str:
        if self.event_id:
            return self.event_id
        # Deterministic id for a source with no canonical event of its own (mission section 14):
        # the SAME (resource_id, type, occurred_at) always yields the SAME eventId, so observing
        # the same underlying change across two poll ticks never mints a duplicate-looking-new id.
        digest = hashlib.sha256(f"{self.resource_id}|{self.type}|{self.occurred_at}".encode()).hexdigest()
        return f"derived:{digest[:32]}"


def to_wire(event: RealtimeEvent, *, sequence: int) -> dict[str, Any]:
    """The exact JSON-serializable dict sent to the browser. Never includes anything beyond
    this fixed field set - a source's `payload` dict is the only place resource-specific data
    lives, and every source is responsible for keeping that payload a typed, minimal projection
    (see each `realtime_sources.py` builder function), never a raw DB row or NATS payload dump."""
    return {
        "schema": SCHEMA,
        "eventId": event.stable_event_id(),
        "type": event.type,
        "occurredAt": event.occurred_at,
        "resource": event.resource,
        "resourceId": event.resource_id,
        "sequence": sequence,
        "payload": event.payload,
    }
