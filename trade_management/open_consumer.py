"""Durable consumer: `signal.entry.created.v1` (`TRADING_CORE`) -> `create_managed_trade`.

**Not activated in production** - this module defines the consumer shape (a handler callable
plus a thin `run_forever` loop over an injected subscribe function, matching
`infrastructure.messaging.jetstream.JetStreamConsumer`'s existing convention); nothing imports
or calls `main()`/starts a process from here, and no entrypoint script wires it up.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from infrastructure.messaging.contracts import EventEnvelope

from .binding import StreamBindingResolver
from .managed_trade import CreationResult, EntrySignalRecordMissing, OPEN_CONSUMER_NAME, create_managed_trade


class ManagedTradeOpenConsumer:
    """Wraps `create_managed_trade` as a durable JetStream handler. `conn_factory` returns a
    fresh connection per message (matching `dataplane`-style consumers elsewhere in this
    codebase's history) so a crash mid-handling never leaves a half-open transaction reused by
    the next message."""

    def __init__(self, conn_factory: Callable[[], Any], resolver: StreamBindingResolver, *,
                consumer_name: str = OPEN_CONSUMER_NAME, max_creation_lag_seconds: float | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.conn_factory = conn_factory
        self.resolver = resolver
        self.consumer_name = consumer_name
        self.max_creation_lag_seconds = max_creation_lag_seconds
        self.clock = clock

    def handle_envelope(self, envelope: EventEnvelope) -> CreationResult:
        signal_id = str(envelope.payload.get("signal_id") or envelope.aggregate_id)
        claimed_hash = envelope.payload.get("entry_signal_hash")
        conn = self.conn_factory()
        return create_managed_trade(conn, event_id=envelope.event_id, signal_id=signal_id,
                                    resolver=self.resolver, now_utc=self.clock(),
                                    consumer_name=self.consumer_name,
                                    max_creation_lag_seconds=self.max_creation_lag_seconds,
                                    claimed_entry_signal_hash=claimed_hash)

    def handle_payload(self, payload: bytes) -> CreationResult:
        envelope = EventEnvelope(**json.loads(payload.decode("utf-8")))
        return self.handle_envelope(envelope)

    async def run_forever(self, consume: Callable[[Callable[[bytes], Awaitable[Any]]], Awaitable[Any]]) -> Any:
        async def _handler(payload: bytes) -> bool:
            try:
                self.handle_payload(payload)
                return True
            except EntrySignalRecordMissing:
                return False  # caller/framework applies retry-with-backoff, then quarantine
        return await consume(_handler)
