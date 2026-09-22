from __future__ import annotations

import asyncio
import unittest

from dataplane.fakes import FailingJetStream
from dataplane.realtime_publisher import RealtimePublicationFailed, RealtimeSignalPublisher
from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.testing import InMemoryJetStream


def raw(**overrides):
    base = {
        "signal_id": "sig-rt-1", "strategy_id": "STRAT", "strategy_version": "V1",
        "strategy_instance_id": "inst", "source_event_id": "event-1", "symbol": "XAUUSDm",
        "canonical_symbol": "XAUUSD", "direction": "LONG", "signal_timestamp": "2026-09-21T00:00:00Z",
        "created_at": "2026-09-21T00:00:01Z", "signal_emitted_at": "2026-09-21T00:00:01Z",
        "entry_mechanisms": ["DEPTH_ONLY"], "entry_price": 100, "stop_price": 99, "target_price": 102,
        "provenance": {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"},
    }
    return {**base, **overrides}


class RealtimePublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_publish_requires_puback_and_reports_acceptance(self):
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        result = await publisher.publish(raw())
        self.assertEqual(result.status, "REALTIME_SIGNAL_ACCEPTED")
        self.assertEqual(result.signal_id, "sig-rt-1")
        self.assertEqual(result.event_id, "sig-rt-1:entry.accepted")
        self.assertEqual(len(js.messages["realtime.signal.entry.accepted.v1"]), 1)

    async def test_deterministic_signal_identity_across_two_publishes_of_the_same_record(self):
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        first = await publisher.publish(raw())
        second = await publisher.publish(raw())
        self.assertEqual(first.signal_id, second.signal_id)
        self.assertEqual(first.entry_signal_hash, second.entry_signal_hash)
        self.assertEqual(first.event_id, second.event_id)

    async def test_publish_failure_is_never_reported_as_accepted_and_never_silently_dropped(self):
        js = FailingJetStream()
        js.available = False
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        with self.assertRaises(RealtimePublicationFailed) as ctx:
            await publisher.publish(raw())
        self.assertEqual(ctx.exception.signal_id, "sig-rt-1")
        self.assertEqual(len(js.messages["realtime.signal.entry.accepted.v1"]), 0)

    async def test_postgres_is_never_imported_or_required_by_this_module(self):
        import ast
        import dataplane.realtime_publisher as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertFalse(any(name == "postgres" or name.startswith("postgres.") for name in imported))
        self.assertNotIn("psycopg", imported)

    async def test_canonicalize_without_publish_lets_caller_record_t1_before_the_network_call(self):
        js = InMemoryJetStream()
        publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
        canonical = publisher.canonicalize(raw())
        calls = []
        result = await publisher.publish(raw(), canonical=canonical, on_publish_initiated=lambda: calls.append("t2"))
        self.assertEqual(calls, ["t2"])
        self.assertEqual(result.entry_signal_hash, canonical.entry_signal_hash)


if __name__ == "__main__":
    unittest.main()
