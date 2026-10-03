"""Durable consumer: `signal.entry.created.v1` (`TRADING_CORE`, unmodified) -> `ExecutionWorker.
process_signal`. Mirrors `trade_management.open_consumer.ManagedTradeOpenConsumer`'s exact shape
(handle_envelope/handle_payload/run_forever over an injected subscribe function) - the same
established convention, not a new pattern - but this package never imports
`trade_management`; the shape is reproduced, not shared, to keep the two packages independently
auditable (mission section 6 ownership boundary).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from infrastructure.messaging.contracts import EventEnvelope
from migration.flags import ExecutionAuthorityMode

from ..intent import EntrySignalRecordMissing
from ..worker import ExecutionOutcome, ExecutionWorker
from ..trace import emit as trace_emit


class RealBridgeNotWired(RuntimeError):
    """Defense in depth, not the primary safety mechanism. The primary mechanism is that
    `execution_v2.runtime.service.py` wires `HttpBridgeFenceClient` as `worker.bridge`, and
    `HttpBridgeFenceClient.submit()` INTENTIONALLY NEVER INVOKES the `broker_call` it is passed
    (see that class's own docstring) - the real bridge process decides how to call MT5, not the
    platform. This sentinel exists purely so that if `worker.bridge` were ever misconfigured back
    to something that DOES invoke `broker_call` locally (a regression to the pre-remediation
    architecture), the result is a loud, immediate exception here - never a fabricated response
    the way an "always FILLED" default would (mission section 8)."""


def _real_bridge_not_wired() -> dict[str, Any]:
    raise RealBridgeNotWired("this callable must never be invoked in production: the real bridge process "
                             "(reached over HTTP, see execution_v2.runtime.bridge_client) owns the MT5 "
                             "order-send decision, not the platform process")


class ExecutionSignalConsumer:
    def __init__(self, worker: ExecutionWorker, *, execution_authority_mode: ExecutionAuthorityMode,
                 authority_provider: Callable[[], str] | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.worker = worker
        self.execution_authority_mode = execution_authority_mode
        self.authority_provider = authority_provider
        self.clock = clock

    def handle_envelope(self, envelope: EventEnvelope) -> ExecutionOutcome:
        signal_id = str(envelope.payload.get("signal_id") or envelope.aggregate_id)
        trace_emit("EXECUTION_EVENT_RECEIVED", signal_id=signal_id, event_id=envelope.event_id,
                   signal_emitted_at=envelope.payload.get("signal_emitted_at"))
        # The ONLY place this boolean is computed - read once from the config-derived mode
        # captured at construction time, never re-derived per message, never inferred from the
        # envelope itself (mission section 2: an EntrySignal's existence never implies permission).
        mode = self.authority_provider() if self.authority_provider is not None else self.execution_authority_mode.value
        authority_enabled = mode == ExecutionAuthorityMode.ENABLED.value
        outcome = self.worker.process_signal(signal_id, execution_authority_enabled=authority_enabled,
                                             broker_call=_real_bridge_not_wired, now_utc=self.clock())
        trace_emit("EXECUTION_EVENT_COMPLETED", signal_id=signal_id, event_id=envelope.event_id,
                   outcome=outcome.result_outcome or outcome.status)
        return outcome

    def handle_payload(self, payload: bytes) -> ExecutionOutcome:
        envelope = EventEnvelope(**json.loads(payload.decode("utf-8")))
        return self.handle_envelope(envelope)

    async def run_forever(self, consume: Callable[[Callable[[bytes], Awaitable[Any]]], Awaitable[Any]]) -> Any:
        async def _handler(payload: bytes) -> bool:
            try:
                self.handle_payload(payload)
                return True
            except EntrySignalRecordMissing:
                return False  # caller applies retry-with-backoff, matching P4's open consumer
        return await consume(_handler)
