from __future__ import annotations

import unittest

from dataplane.fakes import FakeConnection
from dataplane.realtime_publisher import RealtimeSignalPublisher
from dataplane.signal_projector import SignalPersistenceProjector, SignalProjectionConflict
from dataplane.wire import decode_envelope
from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.testing import InMemoryJetStream
from migration.signal import load_entry_mechanisms


def raw(**overrides):
    base = {
        "signal_id": "sig-proj-1", "strategy_id": "STRAT", "strategy_version": "V1",
        "strategy_instance_id": "inst", "source_event_id": "event-1", "symbol": "XAUUSDm",
        "canonical_symbol": "XAUUSD", "direction": "LONG", "signal_timestamp": "2026-09-21T00:00:00Z",
        "created_at": "2026-09-21T00:00:01Z", "signal_emitted_at": "2026-09-21T00:00:01Z",
        "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK"], "entry_price": 100, "stop_price": 99,
        "target_price": 102, "economic_position_id": "epos-1",
        "provenance": {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"},
    }
    return {**base, **overrides}


async def publish_via_wire(js: InMemoryJetStream, payload: dict) -> EventEnvelope:
    publisher = RealtimeSignalPublisher(JetStreamPublisher(js))
    await publisher.publish(payload)
    pending = js.messages["realtime.signal.entry.accepted.v1"][-1]
    return decode_envelope(pending.payload)


class ProjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_projector_materializes_full_relational_record_from_the_wire_payload_alone(self):
        js = InMemoryJetStream()
        envelope = await publish_via_wire(js, raw())
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        inserted = await projector.handle(envelope)
        self.assertTrue(inserted)
        row = conn.signal_row("sig-proj-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["entry_price"], 100)
        self.assertEqual(row["economic_position_id"], "epos-1")
        self.assertEqual(load_entry_mechanisms(conn, "sig-proj-1"), ("DEPTH_ONLY", "REJECTION_WICK"))
        # ingest_signal's own outbox rows (candidate.detected + entry.created) exist so the
        # EXISTING OutboxRelay can still deliver the DB-first reporting event downstream.
        event_types = {r["event_type"] for r in conn.outbox_rows()}
        self.assertEqual(event_types, {"strategy.candidate.detected.v1", "signal.entry.created.v1"})

    async def test_redelivery_of_the_identical_message_produces_one_logical_record(self):
        js = InMemoryJetStream()
        envelope = await publish_via_wire(js, raw())
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        first = await projector.handle(envelope)
        second = await projector.handle(envelope)  # simulated at-least-once redelivery
        third = await projector.handle(envelope)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(third)
        self.assertEqual(len(conn.tables["strategy.entry_signals"]), 1)
        self.assertEqual(projector.metrics.processed_new, 1)
        self.assertEqual(projector.metrics.processed_duplicate_inbox, 2)

    async def test_redelivery_after_a_crash_before_inbox_commit_still_converges_to_one_record(self):
        """Simulates: first delivery raises mid-transaction (crash), so inbox claim + entry_signals
        insert are both rolled back together; the SECOND delivery (JetStream redelivers because
        no ack occurred) completes cleanly and is the only committed row."""
        js = InMemoryJetStream()
        envelope = await publish_via_wire(js, raw())
        conn = FakeConnection()
        calls = {"n": 0}

        def flaky_begin():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after inbox claim, before ingest_signal commits")

        projector = SignalPersistenceProjector(lambda: conn, on_begin=flaky_begin)
        with self.assertRaises(RuntimeError):
            await projector.handle(envelope)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)
        self.assertIsNone(conn.signal_row("sig-proj-1"))  # nothing partially persisted
        inserted = await projector.handle(envelope)  # redelivery
        self.assertTrue(inserted)
        self.assertIsNotNone(conn.signal_row("sig-proj-1"))
        self.assertEqual(len(conn.tables["strategy.entry_signals"]), 1)

    async def test_two_different_signals_never_collide(self):
        js = InMemoryJetStream()
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        first = await projector.handle(await publish_via_wire(js, raw(signal_id="sig-a")))
        second = await projector.handle(await publish_via_wire(js, raw(signal_id="sig-b")))
        self.assertTrue(first); self.assertTrue(second)
        self.assertEqual(set(conn.tables["strategy.entry_signals"]), {"sig-a", "sig-b"})

    async def test_genuine_content_conflict_on_the_same_signal_id_is_surfaced_never_overwritten(self):
        """A signal_id reused for different canonical content (e.g. a producer bug, or a
        collision between DB_FIRST and NATS_FIRST both handling the "same" id differently) must
        never silently overwrite the first persisted record."""
        js = InMemoryJetStream()
        conn = FakeConnection()
        projector = SignalPersistenceProjector(lambda: conn)
        first_envelope = await publish_via_wire(js, raw())
        await projector.handle(first_envelope)
        conflicting_payload = raw(target_price=999)  # same signal_id, different geometry
        conflicting_envelope = EventEnvelope(
            event_id="sig-proj-1:entry.accepted.retry", event_type=first_envelope.event_type,
            aggregate_type="signal", aggregate_id="sig-proj-1", aggregate_version=1,
            occurred_at=first_envelope.occurred_at, payload=conflicting_payload,
        )
        with self.assertRaises(SignalProjectionConflict):
            await projector.handle(conflicting_envelope)
        self.assertEqual(projector.metrics.identity_conflicts, 1)
        row = conn.signal_row("sig-proj-1")
        self.assertEqual(row["target_price"], 102)  # original content, unchanged

    async def test_projector_never_imports_a_specific_broker_or_execution_module(self):
        import dataplane.signal_projector as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("contracts.mt5_bridge", "live_execution_consumer", "trade_manager", "execution.demo_broker"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
