"""Scenarios A-G from the mission, each as an explicit, named test."""
from __future__ import annotations

import unittest

from dataplane.distribution import RealtimeConsumer
from dataplane.fakes import FailingJetStream, FakeConnection
from dataplane.realtime_publisher import RealtimePublicationFailed, RealtimeSignalPublisher
from dataplane.signal_projector import SignalPersistenceProjector
from dataplane.wire import decode_envelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.testing import InMemoryJetStream


def raw(**overrides):
    base = {
        "signal_id": "sig-fail-1", "strategy_id": "STRAT", "strategy_version": "V1",
        "strategy_instance_id": "inst", "source_event_id": "event-1", "symbol": "XAUUSDm",
        "canonical_symbol": "XAUUSD", "direction": "LONG", "signal_timestamp": "2026-09-21T00:00:00Z",
        "created_at": "2026-09-21T00:00:01Z", "signal_emitted_at": "2026-09-21T00:00:01Z",
        "entry_mechanisms": [], "entry_price": 100, "stop_price": 99, "target_price": 102,
        "provenance": {},
    }
    return {**base, **overrides}


class FailureScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_A_postgres_unavailable_real_time_distribution_continues_if_jetstream_is_healthy(self):
        """A: PostgreSQL unavailable. Goal: real-time signal distribution can continue if
        JetStream is healthy. The publisher and distribution consumer are exercised with NO
        PostgreSQL connection constructed anywhere in the flow - not "unreachable", but
        genuinely absent from the call graph."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        received = []
        distribution = RealtimeConsumer(consumer_name="distribution-service", on_deliver=received.append)
        result = await publisher.publish(raw())
        message = await js.next("realtime.signal.entry.accepted.v1")
        delivered = await distribution.handle(message.payload)
        self.assertEqual(result.status, "REALTIME_SIGNAL_ACCEPTED")
        self.assertTrue(delivered)
        self.assertEqual(len(received), 1)

    async def test_B_postgres_projector_crashes_jetstream_retains_events_and_projector_catches_up(self):
        """B: projector crashes. Goal: JetStream retains events; the projector catches up."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())
        message = js.messages["realtime.signal.entry.accepted.v1"][0]  # not acked: "projector crashed" before consuming
        self.assertEqual(len(js.messages["realtime.signal.entry.accepted.v1"]), 1)  # still retained
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        inserted = await projector.handle(decode_envelope(message.payload))  # projector restarts and catches up
        self.assertTrue(inserted)
        self.assertIsNotNone(conn.signal_row("sig-fail-1"))

    async def test_C_projector_receives_the_same_signal_twice_one_logical_db_record(self):
        """C: duplicate delivery. Goal: one logical DB record."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())
        envelope = decode_envelope(js.messages["realtime.signal.entry.accepted.v1"][0].payload)
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        await projector.handle(envelope)
        await projector.handle(envelope)
        await projector.handle(envelope)
        self.assertEqual(len(conn.tables["strategy.entry_signals"]), 1)

    async def test_D_jetstream_unavailable_signal_cannot_be_considered_successfully_published(self):
        """D: JetStream unavailable. Goal: the signal cannot be considered successfully
        published - never a silent success, and identity is preserved for a later retry."""
        js = FailingJetStream()
        js.available = False
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        with self.assertRaises(RealtimePublicationFailed) as ctx:
            await publisher.publish(raw())
        self.assertEqual(ctx.exception.signal_id, "sig-fail-1")
        js.available = True  # recovery
        result = await publisher.publish(raw())  # retry with the identical record
        self.assertEqual(result.status, "REALTIME_SIGNAL_ACCEPTED")
        self.assertEqual(result.signal_id, "sig-fail-1")  # identity preserved across the outage

    async def test_E_subscriber_delivery_consumer_crashes_durable_recovery_via_redelivery(self):
        """E: a distribution consumer crashes mid-handling. Goal: appropriate durable
        recovery/redelivery - the crashed delivery does not silently disappear, and a later
        (re)delivery of the SAME message still delivers exactly once to this consumer."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())
        payload = js.messages["realtime.signal.entry.accepted.v1"][0].payload

        received, calls = [], {"n": 0}

        def flaky(envelope):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated distribution consumer crash mid-delivery")
            received.append(envelope)

        distribution = RealtimeConsumer(consumer_name="distribution-service", on_deliver=flaky)
        with self.assertRaises(RuntimeError):
            await distribution.handle(payload)  # crash: message not marked delivered
        self.assertEqual(distribution.metrics.delivered, 0)
        delivered = await distribution.handle(payload, redelivered=True)  # JetStream redelivers (no ack occurred)
        self.assertTrue(delivered)
        self.assertEqual(len(received), 1)
        self.assertEqual(distribution.metrics.redeliveries, 1)

    async def test_F_orchestrator_restarts_after_publish_before_local_completion_no_duplicate_logical_signal(self):
        """F: orchestrator restarts after JetStream accepted the publish but before the
        orchestrator's own local bookkeeping completed. Goal: deterministic ID + JetStream
        dedupe/idempotency prevent a duplicate LOGICAL signal - simulated here as: the
        orchestrator re-attempts publish() for the same accepted StrategySignal after "restart"
        (it does not know whether the first attempt's PUBACK was received), and the
        projector still produces exactly one persisted EntrySignal."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        first_result = await publisher.publish(raw())  # orchestrator crashes right after this returns, before it durably records "done" locally
        second_result = await publisher.publish(raw())  # restart: re-publishes the identical StrategySignal, unsure if the first landed
        self.assertEqual(first_result.event_id, second_result.event_id)
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        for message in list(js.messages["realtime.signal.entry.accepted.v1"]):
            await projector.handle(decode_envelope(message.payload))
        self.assertEqual(len(conn.tables["strategy.entry_signals"]), 1)  # one logical signal, not two

    async def test_G_postgres_lags_substantially_subscriber_delivery_and_realtime_consumers_stay_independent(self):
        """G: PostgreSQL lags substantially. Goal: subscriber delivery and real-time consumers
        remain independent - distribution receives and delivers immediately even though the
        projector has not run at all yet."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        received = []
        distribution = RealtimeConsumer(consumer_name="distribution-service", on_deliver=received.append)
        await publisher.publish(raw())
        payload = js.messages["realtime.signal.entry.accepted.v1"][0].payload
        delivered = await distribution.handle(payload)
        self.assertTrue(delivered)
        self.assertEqual(len(received), 1)
        # PostgreSQL/the projector has still done nothing at this point - no connection was
        # even constructed - and delivery already happened.


if __name__ == "__main__":
    unittest.main()
