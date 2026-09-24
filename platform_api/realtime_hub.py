"""In-process realtime fan-out hub (mission sections 5-8, 15, 17). One `RealtimeHub` instance
lives per server process and is shared by every WebSocket connection - this is the "backend
fan-out from controlled internal consumers" the mission asks for (section 17): the NATS/DB
sources in `realtime_sources.py` publish exactly ONCE per change, and the hub fans that single
publish out to every subscribed connection's own bounded queue. A slow or disconnected browser
never creates a second NATS consumer, never blocks another browser, and never grows backend
memory without bound.

Per-resource state:
  - a monotonically increasing `sequence` counter (assigned at publish time)
  - a bounded ring buffer of the most recent events, used to serve `resume` requests without
    requiring a client to fall back to a full snapshot refetch for a short disconnect
  - the set of connections currently subscribed

Gap/resume semantics (section 6): a connection asks to resume a resource `since` some sequence
it last saw. If that sequence is still covered by the ring buffer, the hub replays exactly the
missed events. If it has aged out (long disconnect, or the process restarted and the buffer is
empty), the hub tells the connection `resync_required` and the CLIENT is responsible for falling
back to the REST snapshot - the hub never silently pretends nothing happened while it was gone.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .realtime_envelope import RESOURCES, RealtimeEvent, to_wire

log = logging.getLogger("platform_api.realtime")

RING_BUFFER_SIZE = 500  # events retained per resource for resume; well beyond one poll interval
CONNECTION_QUEUE_SIZE = 500  # bounded outbound queue per connection (section 15 backpressure)


@dataclass
class HubMetrics:
    """Mission section 18: enough to answer "how healthy is the realtime path" without exposing
    anything sensitive. `snapshot()` is what `/realtime/health` returns."""

    connections_active: int = 0
    connections_total: int = 0
    disconnections_total: int = 0
    events_published_total: int = 0
    events_delivered_total: int = 0
    events_dropped_total: int = 0  # backpressure-coalesced or slow-consumer drops
    resyncs_required_total: int = 0
    slow_consumer_disconnects_total: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "connections_active": self.connections_active,
            "connections_total": self.connections_total,
            "disconnections_total": self.disconnections_total,
            "events_published_total": self.events_published_total,
            "events_delivered_total": self.events_delivered_total,
            "events_dropped_total": self.events_dropped_total,
            "resyncs_required_total": self.resyncs_required_total,
            "slow_consumer_disconnects_total": self.slow_consumer_disconnects_total,
        }


@dataclass
class _ResourceState:
    sequence: int = 0
    buffer: deque[tuple[int, dict[str, Any]]] = field(default_factory=lambda: deque(maxlen=RING_BUFFER_SIZE))
    subscribers: set["ConnectionHandle"] = field(default_factory=set)


class SlowConsumerError(RuntimeError):
    """Raised against one connection's send loop when it has fallen too far behind and must be
    disconnected - never allowed to propagate to, or block, any other connection or source."""


class ConnectionHandle:
    """One WebSocket connection's server-side state: its bounded outbound queue and which
    resources it currently subscribes to. The transport layer (`realtime.py`) owns the actual
    socket; this class owns only queueing/subscription bookkeeping so it can be unit tested
    without a real WebSocket."""

    __slots__ = ("connection_id", "queue", "resources", "dropped_since_warned")

    def __init__(self, connection_id: str) -> None:
        self.connection_id = connection_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CONNECTION_QUEUE_SIZE)
        self.resources: set[str] = set()
        self.dropped_since_warned = 0

    def __hash__(self) -> int:
        return hash(self.connection_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ConnectionHandle) and other.connection_id == self.connection_id

    def offer(self, message: dict[str, Any]) -> bool:
        """Non-blocking enqueue. Returns False (and the caller counts a drop) if this
        connection's queue is already full - a slow browser never blocks the publisher or any
        other connection (section 15)."""
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False


class RealtimeHub:
    def __init__(self, *, metrics: HubMetrics | None = None, clock: Any = time.time) -> None:
        self._resources = {name: _ResourceState() for name in RESOURCES}
        self._connections: dict[str, ConnectionHandle] = {}
        self.metrics = metrics or HubMetrics()
        self._clock = clock

    # -- connection lifecycle -------------------------------------------------------------
    def register(self, connection_id: str) -> ConnectionHandle:
        handle = ConnectionHandle(connection_id)
        self._connections[connection_id] = handle
        self.metrics.connections_active += 1
        self.metrics.connections_total += 1
        return handle

    def unregister(self, handle: ConnectionHandle) -> None:
        for state in self._resources.values():
            state.subscribers.discard(handle)
        self._connections.pop(handle.connection_id, None)
        self.metrics.connections_active = max(0, self.metrics.connections_active - 1)
        self.metrics.disconnections_total += 1

    # -- subscription -----------------------------------------------------------------------
    def subscribe(self, handle: ConnectionHandle, resource: str) -> int:
        """Returns the resource's CURRENT latest sequence at the moment of subscription - the
        boundary the client uses to reconcile its REST snapshot (section 5): the snapshot,
        fetched AFTER this call returns, is guaranteed to reflect everything up to and including
        this sequence, because a sequence is only assigned once the underlying change is already
        durably committed at its source of truth."""
        if resource not in self._resources:
            raise ValueError(f"unknown resource: {resource!r}")
        self._resources[resource].subscribers.add(handle)
        handle.resources.add(resource)
        return self._resources[resource].sequence

    def unsubscribe(self, handle: ConnectionHandle, resource: str) -> None:
        state = self._resources.get(resource)
        if state is not None:
            state.subscribers.discard(handle)
        handle.resources.discard(resource)

    # -- resume -----------------------------------------------------------------------------
    def replay_since(self, resource: str, since_sequence: int) -> list[dict[str, Any]] | None:
        """Returns the wire messages strictly after `since_sequence`, or None if the ring buffer
        no longer covers that point (the caller must then tell the client `resync_required`)."""
        state = self._resources[resource]
        if since_sequence >= state.sequence:
            return []  # already fully caught up
        buffered = list(state.buffer)
        if not buffered or buffered[0][0] > since_sequence + 1:
            # The oldest buffered sequence is already past what the client needs - there's a gap
            # the buffer cannot fill.
            self.metrics.resyncs_required_total += 1
            return None
        return [msg for seq, msg in buffered if seq > since_sequence]

    def current_sequence(self, resource: str) -> int:
        return self._resources[resource].sequence

    # -- publish ------------------------------------------------------------------------------
    def publish(self, event: RealtimeEvent) -> dict[str, Any]:
        """Assigns the next sequence for `event.resource`, buffers it, and offers it to every
        currently-subscribed connection's queue. A full connection queue counts as a drop for
        THAT connection only (section 15) - it never blocks this call or affects any other
        subscriber. Returns the wire message that was published (useful for tests/logging)."""
        state = self._resources[event.resource]
        state.sequence += 1
        message = to_wire(event, sequence=state.sequence)
        state.buffer.append((state.sequence, message))
        self.metrics.events_published_total += 1

        for handle in list(state.subscribers):
            if handle.offer(message):
                self.metrics.events_delivered_total += 1
            else:
                self.metrics.events_dropped_total += 1
                handle.dropped_since_warned += 1
                log.warning("realtime queue full, dropping event for connection %s resource=%s "
                           "dropped_since_warned=%d", handle.connection_id, event.resource,
                           handle.dropped_since_warned)
        return message
