"""Real JetStream wiring for `trade_management.open_consumer.ManagedTradeOpenConsumer`
(mission section 1). The activation boundary (no replay of the 9 pre-existing canonical
signals) is enforced by JetStream itself: `DeliverPolicy.NEW` is used when the durable consumer
is created for the *first* time, so JetStream never delivers anything already in the stream at
that instant. `trade_management/runtime/activation.py` persists the proof of when that happened.
"""
from __future__ import annotations

import uuid
from typing import Any

from trade_management.managed_trade import EntrySignalRecordMissing
from trade_management.open_consumer import ManagedTradeOpenConsumer

from .activation import establish_activation_boundary

SUBJECT = "signal.entry.created.v1"
STREAM = "TRADING_CORE"


async def bootstrap_open_consumer(js_manager: Any, conn: Any, *, consumer_name: str) -> dict[str, Any]:
    """Idempotent: creates the durable consumer only if it does not already exist (never
    recreates it - recreating a durable consumer with DeliverPolicy.NEW a second time would
    silently move the activation boundary forward, exactly what must never happen). Returns the
    persisted (first-write-wins) activation boundary record either way."""
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    try:
        stream_info = await js_manager.stream_info(STREAM)
        await js_manager.consumer_info(STREAM, consumer_name)
        already_existed = True
    except Exception:
        already_existed = False
        stream_info = await js_manager.stream_info(STREAM)
        config = ConsumerConfig(durable_name=consumer_name, ack_policy=AckPolicy.EXPLICIT,
                                deliver_policy=DeliverPolicy.NEW, filter_subject=SUBJECT,
                                deliver_subject=f"_INBOX.{consumer_name}.{uuid.uuid4().hex}",
                                ack_wait=30, max_deliver=-1)
        await js_manager.add_consumer(STREAM, config)

    boundary = establish_activation_boundary(conn, consumer_name=consumer_name, subject=SUBJECT,
                                             stream=STREAM, stream_messages_at_establishment=stream_info.state.messages)
    boundary["consumer_already_existed_at_bootstrap"] = already_existed
    return boundary


def make_open_consumer(conn_factory: Any, resolver: Any, *, consumer_name: str) -> ManagedTradeOpenConsumer:
    return ManagedTradeOpenConsumer(conn_factory, resolver, consumer_name=consumer_name)


async def subscribe_open_consumer(js: Any, consumer: ManagedTradeOpenConsumer, *, consumer_name: str) -> Any:
    async def on_message(msg: Any) -> None:
        try:
            consumer.handle_payload(msg.data)
            await msg.ack()
        except EntrySignalRecordMissing:
            # Retry with backoff, per the domain contract (A7 10): never ack, never create from
            # the event payload alone. JetStream's own redelivery/backoff handles the retry.
            await msg.nak()
        except Exception:
            await msg.nak()

    return await js.subscribe(SUBJECT, stream=STREAM, durable=consumer_name, manual_ack=True, cb=on_message)
