from __future__ import annotations

import unittest

from dataplane.distribution import RealtimeConsumer
from dataplane.realtime_publisher import RealtimeSignalPublisher
from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.testing import InMemoryJetStream


def raw(**overrides):
    base = {
        "signal_id": "sig-dist-1", "strategy_id": "STRAT", "strategy_version": "V1",
        "strategy_instance_id": "inst", "source_event_id": "event-1", "symbol": "XAUUSDm",
        "canonical_symbol": "XAUUSD", "direction": "LONG", "signal_timestamp": "2026-09-21T00:00:00Z",
        "created_at": "2026-09-21T00:00:01Z", "signal_emitted_at": "2026-09-21T00:00:01Z",
        "entry_mechanisms": [], "entry_price": 100, "stop_price": 99, "target_price": 102,
        "provenance": {},
    }
    return {**base, **overrides}


class DistributionAndTradeManagerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_distribution_consumer_receives_a_signal_with_no_postgres_involved(self):
        import dataplane.distribution as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("postgres", source)

        js = InMemoryJetStream()
        received = []
        distribution = RealtimeConsumer(consumer_name="distribution-service", on_deliver=received.append)
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())
        message = await js.next("realtime.signal.entry.accepted.v1")
        delivered = await distribution.handle(message.payload)
        await js.ack(message)
        self.assertTrue(delivered)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].aggregate_id, "sig-dist-1")

    async def test_trade_manager_intake_is_the_same_consumer_shape_and_is_independent_of_distribution(self):
        """Demonstrates the architectural boundary only: a Trade-Manager-shaped consumer can
        attach to the SAME real-time subject as an independent durable consumer group and
        process without waiting on the distribution consumer or on PostgreSQL. No Trade Manager
        decision logic is implemented or activated - `on_decision_input` stands in for "hand off
        to the (not-built-here) TradeManagerDecision evaluator"."""
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())

        distribution_seen, tm_seen = [], []
        distribution = RealtimeConsumer(consumer_name="distribution-service", on_deliver=distribution_seen.append)
        trade_manager_intake = RealtimeConsumer(consumer_name="trade-manager-intake", on_deliver=tm_seen.append)

        # Both consumers read the SAME published message (JetStream durable consumers are
        # independent cursors over one stream); the fake harness exposes one queue per subject,
        # so we simulate two independent cursors by peeking without acking for each consumer.
        message = js.messages["realtime.signal.entry.accepted.v1"][0]
        await distribution.handle(message.payload)
        await trade_manager_intake.handle(message.payload)

        self.assertEqual(len(distribution_seen), 1)
        self.assertEqual(len(tm_seen), 1)
        self.assertEqual(distribution.consumer_name, "distribution-service")
        self.assertEqual(trade_manager_intake.consumer_name, "trade-manager-intake")
        # Neither consumer's metrics or delivered set is shared with the other.
        self.assertIsNot(distribution._delivered_event_ids, trade_manager_intake._delivered_event_ids)

    async def test_trade_manager_is_never_activated_and_no_decision_module_is_imported(self):
        import ast
        import dataplane.distribution as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("trade_manager", "trade_manager.engine", "trade_manager.phase2",
                          "live_execution_consumer", "contracts.mt5_bridge"):
            self.assertNotIn(forbidden, imported)

    async def test_distribution_consumer_dedupes_its_own_redelivery(self):
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        await publisher.publish(raw())
        message = js.messages["realtime.signal.entry.accepted.v1"][0]
        distribution = RealtimeConsumer(consumer_name="distribution-service")
        first = await distribution.handle(message.payload)
        second = await distribution.handle(message.payload, redelivered=True)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(distribution.metrics.received, 2)
        self.assertEqual(distribution.metrics.delivered, 1)
        self.assertEqual(distribution.metrics.redeliveries, 1)


if __name__ == "__main__":
    unittest.main()
