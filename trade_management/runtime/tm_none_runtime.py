"""Real JetStream wiring for TM-NONE-1 (mission section 4): a durable consumer on
`trade.observation.recorded.v1` (`TRADING_OBSERVATION`) that invokes
`trade_management.tm_none.record_decision` for every observation. Every eligible observation
yields a persisted `HOLD`; the publication gate then withholds it
(`NOT_ACTIONABLE_HOLD`) - no `ManagementSignal`, no customer distribution, both unimplemented by
design (mission section 11).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from infrastructure.messaging.contracts import EventEnvelope
from trade_management.tm_none import DECISION_CONSUMER_NAME, record_decision

SUBJECT = "trade.observation.recorded.v1"
STREAM = "TRADING_OBSERVATION"


def decode_envelope(payload: bytes) -> EventEnvelope:
    return EventEnvelope(**json.loads(payload.decode("utf-8")))


async def bootstrap_tm_none_consumer(js_manager: Any, *, consumer_name: str) -> None:
    """Idempotent. `DeliverPolicy.ALL` here is deliberate and different from the open consumer:
    TM-NONE must evaluate every observation on `TRADING_OBSERVATION` (a stream P4 itself owns
    and just created), not skip whatever was recorded between stream creation and this
    consumer's own bootstrap - there is no "pre-existing production history" concern on a
    brand-new P4-owned stream the way there is on TRADING_CORE."""
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    try:
        await js_manager.consumer_info(STREAM, consumer_name)
        return
    except Exception:
        pass
    config = ConsumerConfig(durable_name=consumer_name, ack_policy=AckPolicy.EXPLICIT,
                            deliver_policy=DeliverPolicy.ALL, filter_subject=SUBJECT,
                            deliver_subject=f"_INBOX.{consumer_name}.{uuid.uuid4().hex}",
                            ack_wait=30, max_deliver=-1)
    await js_manager.add_consumer(STREAM, config)


async def subscribe_tm_none_consumer(js: Any, conn_factory: Callable[[], Any], *,
                                     consumer_name: str = DECISION_CONSUMER_NAME,
                                     clock: Callable[[], Any] | None = None) -> Any:
    from datetime import datetime, timezone
    clock = clock or (lambda: datetime.now(timezone.utc))

    async def on_message(msg: Any) -> None:
        try:
            envelope = decode_envelope(msg.data)
            conn = conn_factory()
            record_decision(conn, observation_id=envelope.event_id, event_id=envelope.event_id,
                            now_utc=clock(), consumer_name=consumer_name)
            await msg.ack()
        except Exception:
            await msg.nak()

    return await js.subscribe(SUBJECT, stream=STREAM, durable=consumer_name, manual_ack=True, cb=on_message)
