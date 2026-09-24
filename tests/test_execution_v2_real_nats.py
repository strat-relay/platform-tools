"""Real-NATS JetStream proof for execution_v2 (mission sections 9/13): the existing, unmodified
EXECUTION stream/subjects (infrastructure/messaging/contracts.py - already registered before this
mission; no contracts.py change was needed) actually accept `execution.intent.created.v1` /
`execution.result.recorded.v1` events published by the real, unmodified
`infrastructure.messaging.outbox_relay.OutboxRelay` against a real JetStream server, and a durable
consumer actually receives them - not merely a mock/Protocol satisfying the type shape.

Skipped automatically when no NATS server is reachable (set V2_TEST_NATS_URL, e.g. via the
ephemeral `nats:2.10-alpine -js` docker run documented in docs/v2_execution/README.md).
"""
from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from datetime import datetime, timezone

from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher, JetStreamTopology
from infrastructure.messaging.outbox_relay import OutboxRelay

NATS_URL = os.getenv("V2_TEST_NATS_URL", "nats://localhost:14245")


def _nats_available() -> bool:
    try:
        import nats
    except ImportError:
        return False

    async def _probe() -> bool:
        try:
            nc = await nats.connect(NATS_URL, connect_timeout=2)
            await nc.close()
            return True
        except Exception:
            return False

    return asyncio.run(_probe())


class _FakeOutboxConn:
    """Minimal in-process stand-in for the PostgreSQL outbox rows OutboxRelay reads/updates -
    isolates this test from needing a live PostgreSQL just to prove the JetStream leg; the
    combined real-PostgreSQL-to-real-JetStream path is exercised by hand in
    docs/v2_execution/README.md's manual end-to-end proof, since running both ephemeral
    containers from one automated test is out of scope for this slice."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, str]] = []  # (event_id, publish_status)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None) -> None:
        self._last_sql = " ".join(sql.split()).upper()
        self._last_params = params

    def fetchall(self):
        if "SELECT" in self._last_sql:
            rows, self._rows = self._rows, []
            return rows
        return []

    def commit(self) -> None:
        pass


@unittest.skipUnless(_nats_available(), "NATS JetStream is not available; see docs/v2_execution/README.md")
class RealNatsExecutionV2Tests(unittest.TestCase):
    def test_execution_stream_accepts_intent_and_result_events_and_a_durable_consumer_receives_them(self):
        import nats

        async def scenario():
            from nats.js.api import ConsumerConfig, DeliverPolicy

            nc = await nats.connect(NATS_URL)
            js = nc.jetstream()
            await JetStreamTopology.v1().ensure(js)

            publisher = JetStreamPublisher(js)
            intent_event_id = f"EXECV2_TEST_{uuid.uuid4().hex[:12]}:execution.intent.created"
            result_event_id = f"EXECV2_TEST_{uuid.uuid4().hex[:12]}:execution.result.recorded"
            now = datetime.now(timezone.utc)

            intent_envelope = EventEnvelope(intent_event_id, "execution.intent.created.v1", "execution_intent",
                                            "EXECV2_TEST_INTENT", 1, now,
                                            {"execution_intent_id": "EXECV2_TEST_INTENT", "status": "CREATED"},
                                            "SIG_TEST", None)
            result_envelope = EventEnvelope(result_event_id, "execution.result.recorded.v1", "execution_attempt",
                                            "ATT_TEST", 1, now,
                                            {"attempt_id": "ATT_TEST", "outcome": "FILLED"},
                                            "EXECV2_TEST_INTENT", None)

            received: list[bytes] = []
            done = asyncio.Event()

            async def handler(msg):
                received.append(msg.data)
                await msg.ack()
                if len(received) >= 2:
                    done.set()

            # DeliverPolicy.NEW: this shared EXECUTION stream accumulates events from every test
            # in this suite (and from test_execution_v2_real_bridge.py's outbox-relay proofs) -
            # subscribing to only what is published AFTER this point, rather than the default
            # ALL-from-start-of-stream, is what makes "exactly 2 received" a correct assertion
            # regardless of what other tests already published to "execution.>" earlier in the run.
            config = ConsumerConfig(durable_name=f"v2exec_test_{uuid.uuid4().hex[:8]}",
                                    deliver_policy=DeliverPolicy.NEW, filter_subject="execution.>")
            sub = await js.subscribe("execution.>", durable=config.durable_name, manual_ack=True,
                                     cb=handler, config=config)
            try:
                await publisher.publish(intent_envelope)
                await publisher.publish(result_envelope)
                await asyncio.wait_for(done.wait(), timeout=10)
            finally:
                await sub.unsubscribe()
                await nc.close()
            return received

        received = asyncio.run(scenario())
        self.assertEqual(len(received), 2)
        self.assertTrue(any(b"execution_intent" in r for r in received))
        self.assertTrue(any(b"execution_attempt" in r for r in received))

    def test_outbox_relay_publishes_real_execution_v2_rows_through_real_jetstream(self):
        import nats

        async def scenario():
            nc = await nats.connect(NATS_URL)
            js = nc.jetstream()
            await JetStreamTopology.v1().ensure(js)

            event_id = f"EXECV2_RELAY_{uuid.uuid4().hex[:12]}:execution.intent.created"
            now_iso = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            row = (event_id, "execution.intent.created.v1", "execution_intent", "EXECV2_RELAY_INTENT", 1,
                  "event-envelope.v1", {"execution_intent_id": "EXECV2_RELAY_INTENT", "status": "CREATED"},
                  now_iso, "SIG_RELAY_TEST", None)
            conn = _FakeOutboxConn([row])
            relay = OutboxRelay(conn, JetStreamPublisher(js))

            received = asyncio.Event()
            captured: list[bytes] = []

            async def handler(msg):
                if msg.headers and msg.headers.get("Nats-Msg-Id") == event_id:
                    captured.append(msg.data)
                    received.set()
                await msg.ack()

            from nats.js.api import ConsumerConfig, DeliverPolicy
            durable_name = f"v2exec_relay_test_{uuid.uuid4().hex[:8]}"
            config = ConsumerConfig(durable_name=durable_name, deliver_policy=DeliverPolicy.NEW,
                                    filter_subject="execution.intent.created.v1")
            sub = await js.subscribe("execution.intent.created.v1", durable=durable_name,
                                     manual_ack=True, cb=handler, config=config)
            try:
                result = await relay.publish_batch(limit=10)
                await asyncio.wait_for(received.wait(), timeout=10)
            finally:
                await sub.unsubscribe()
                await nc.close()
            return result, captured

        result, captured = asyncio.run(scenario())
        self.assertEqual(result, {"published": 1, "failed": 0})
        self.assertEqual(len(captured), 1)
        self.assertIn(b"EXECV2_RELAY_INTENT", captured[0])


if __name__ == "__main__":
    unittest.main()
