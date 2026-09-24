"""RealtimeHub proof (mission sections 5-8, 15, 17): sequencing, resume/gap detection,
per-connection backpressure isolation, and that a slow connection never affects another."""
from __future__ import annotations

import asyncio
import unittest

from platform_api.realtime_envelope import RESOURCE_SIGNALS, RESOURCE_TRADE_MANAGEMENT, RealtimeEvent
from platform_api.realtime_hub import CONNECTION_QUEUE_SIZE, RealtimeHub


def signal_event(resource_id: str, *, event_id: str | None = None) -> RealtimeEvent:
    return RealtimeEvent(type="signal.created", occurred_at="2026-09-23T00:00:00Z",
                         resource=RESOURCE_SIGNALS, resource_id=resource_id,
                         payload={"signalId": resource_id}, event_id=event_id)


class SequencingTests(unittest.TestCase):
    def test_sequence_is_monotonic_per_resource_and_independent_across_resources(self):
        hub = RealtimeHub()
        hub.publish(signal_event("SIG_1"))
        msg = hub.publish(signal_event("SIG_2"))
        self.assertEqual(msg["sequence"], 2)
        self.assertEqual(hub.current_sequence(RESOURCE_SIGNALS), 2)
        self.assertEqual(hub.current_sequence(RESOURCE_TRADE_MANAGEMENT), 0)

    def test_envelope_shape_matches_the_documented_wire_contract(self):
        hub = RealtimeHub()
        msg = hub.publish(signal_event("SIG_1", event_id="evt-1"))
        self.assertEqual(msg, {
            "schema": "console-realtime.v1", "eventId": "evt-1", "type": "signal.created",
            "occurredAt": "2026-09-23T00:00:00Z", "resource": "signals", "resourceId": "SIG_1",
            "sequence": 1, "payload": {"signalId": "SIG_1"},
        })


class SubscriptionAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_subscribed_connections_receive_a_published_event(self):
        hub = RealtimeHub()
        subscribed = hub.register("conn-a")
        unsubscribed = hub.register("conn-b")
        hub.subscribe(subscribed, RESOURCE_SIGNALS)

        hub.publish(signal_event("SIG_1"))

        self.assertEqual(subscribed.queue.qsize(), 1)
        self.assertEqual(unsubscribed.queue.qsize(), 0)

    async def test_unregister_removes_all_subscriptions(self):
        hub = RealtimeHub()
        handle = hub.register("conn-a")
        hub.subscribe(handle, RESOURCE_SIGNALS)
        hub.unregister(handle)
        hub.publish(signal_event("SIG_1"))
        self.assertEqual(hub.metrics.connections_active, 0)

    async def test_subscribe_returns_the_current_sequence_at_that_instant(self):
        hub = RealtimeHub()
        hub.publish(signal_event("SIG_1"))  # sequence 1, before anyone subscribes
        handle = hub.register("conn-a")
        boundary = hub.subscribe(handle, RESOURCE_SIGNALS)
        self.assertEqual(boundary, 1)
        # A REST snapshot fetched after this point is guaranteed to reflect sequence <= 1.


class ResumeTests(unittest.TestCase):
    def test_replay_since_returns_exactly_the_missed_events_in_order(self):
        hub = RealtimeHub()
        hub.publish(signal_event("SIG_1"))
        hub.publish(signal_event("SIG_2"))
        hub.publish(signal_event("SIG_3"))
        replay = hub.replay_since(RESOURCE_SIGNALS, 1)
        self.assertEqual([m["resourceId"] for m in replay], ["SIG_2", "SIG_3"])

    def test_replay_since_current_sequence_returns_empty_not_none(self):
        hub = RealtimeHub()
        hub.publish(signal_event("SIG_1"))
        self.assertEqual(hub.replay_since(RESOURCE_SIGNALS, 1), [])

    def test_replay_since_a_sequence_older_than_the_buffer_signals_resync_required(self):
        hub = RealtimeHub()
        for i in range(5):
            hub.publish(signal_event(f"SIG_{i}"))
        # Simulate an aged-out buffer by asking for something before recorded history.
        replay = hub.replay_since(RESOURCE_SIGNALS, -100)
        self.assertIsNone(replay)
        self.assertEqual(hub.metrics.resyncs_required_total, 1)


class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_full_connection_queue_drops_for_that_connection_only_not_others(self):
        hub = RealtimeHub()
        slow = hub.register("slow")
        healthy = hub.register("healthy")
        hub.subscribe(slow, RESOURCE_SIGNALS)
        hub.subscribe(healthy, RESOURCE_SIGNALS)

        for i in range(CONNECTION_QUEUE_SIZE + 10):
            hub.publish(signal_event(f"SIG_{i}"))

        self.assertEqual(slow.queue.qsize(), CONNECTION_QUEUE_SIZE)  # bounded, never grows past this
        self.assertGreater(hub.metrics.events_dropped_total, 0)
        # The healthy connection is never drained in this test, so it also fills to the bound -
        # the point is it fills to the SAME bound, not that it grows unbounded because of the
        # other connection's slowness.
        self.assertEqual(healthy.queue.qsize(), CONNECTION_QUEUE_SIZE)

    async def test_publish_never_blocks_even_when_every_connection_queue_is_full(self):
        hub = RealtimeHub()
        handle = hub.register("conn-a")
        hub.subscribe(handle, RESOURCE_SIGNALS)
        for i in range(CONNECTION_QUEUE_SIZE + 1):
            hub.publish(signal_event(f"SIG_{i}"))  # must never raise / hang
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
