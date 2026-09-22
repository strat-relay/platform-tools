"""Real-time consumers that never depend on PostgreSQL: signal distribution and the Trade
Manager intake boundary.

Both are the SAME generic shape (`RealtimeConsumer`): a durable, independently-acking
subscriber to `realtime.signal.entry.accepted.v1` with no PostgreSQL import anywhere in this
module. This is deliberate: the architectural point (docs/nats_first_data_plane, task section 9
and 10) is that customer delivery and Trade Manager intake are BOTH just consumers of the same
durable stream, decoupled from each other and from the projector.

This module does NOT build the Customer Portal, a WebSocket server, subscriptions, billing, or
Trade Manager decision logic. `on_deliver`/`on_decision_input` are injected callables standing
in for "push to a subscriber" / "hand off to the Trade Manager evaluator" respectively - the
architectural boundary, not the product behind it. Trade Manager is never activated by this
module; nothing here evaluates a TradeManagerDecision.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from infrastructure.messaging.contracts import EventEnvelope
from dataplane.wire import decode_envelope


@dataclass
class ConsumerMetrics:
    received: int = 0
    delivered: int = 0
    redeliveries: int = 0
    failures: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class RealtimeConsumer:
    """A durable, PostgreSQL-independent consumer of the real-time signal subject.

    Used both as the (prototype) Signal Distribution Service and as the demonstration of Trade
    Manager intake compatibility (see tests/test_dataplane_distribution_and_trade_manager.py):
    the two are simply two independently-durable instances of this class, consuming the same
    subject under different `consumer_name`s, so neither can block or be blocked by the other,
    or by the projector.
    """

    def __init__(self, *, consumer_name: str, on_deliver: Callable[[EventEnvelope], Any] | None = None,
                 on_receive: Callable[[], None] | None = None):
        self.consumer_name = consumer_name
        self.on_deliver = on_deliver
        self.on_receive = on_receive
        self.metrics = ConsumerMetrics()
        self._delivered_event_ids: set[str] = set()

    async def handle(self, payload: bytes, *, redelivered: bool = False) -> bool:
        self.metrics.received += 1
        self.metrics.redeliveries += int(redelivered)
        if self.on_receive is not None:
            self.on_receive()
        try:
            envelope = decode_envelope(payload)
        except Exception:
            self.metrics.failures += 1
            raise
        # Consumer-local de-dup (this class carries no external inbox by design: it is
        # intentionally lightweight and stateless-restart-tolerant via JetStream's own durable
        # consumer position; a production distribution/TM-intake service may still choose a
        # persistent inbox, which is orthogonal to this architectural boundary). The dedup
        # marker is recorded ONLY after on_deliver succeeds - never before - so a crash or
        # exception inside on_deliver leaves this message eligible for redelivery, rather than
        # repeating the "checkpoint saved before processing" at-most-once defect this whole
        # migration lineage (A4/A6/A7) identified and moved away from in the legacy fan-out.
        if envelope.event_id in self._delivered_event_ids:
            return False
        if self.on_deliver is not None:
            result = self.on_deliver(envelope)
            if inspect.isawaitable(result):
                await result
        self._delivered_event_ids.add(envelope.event_id)
        self.metrics.delivered += 1
        return True

    async def run_forever(self, consume: Callable[[Callable[[bytes], Awaitable[bool]]], Awaitable[Any]]) -> Any:
        return await consume(self.handle)
