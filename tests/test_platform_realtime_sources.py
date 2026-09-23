"""Source translation proof (mission section 14): the NATS-backed sources translate the two
REAL canonical domain events honestly; the bounded poller detects changes for the three
resources that have no canonical event, without ever inventing a fake NATS event type, and
without ever re-emitting the same underlying change as a new-looking eventId across ticks."""
from __future__ import annotations

import asyncio
import json
import unittest

from platform_api.realtime_envelope import RESOURCE_SIGNALS, RESOURCE_SYSTEM, RESOURCE_TRADE_MANAGEMENT
from platform_api.realtime_hub import RealtimeHub
from platform_api.realtime_sources import BoundedChangePoller, NatsObservationSource, NatsSignalSource


class FakeJetStreamMessages:
    """Duck-typed like a nats-py subscription's `.messages` async iterator."""

    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = payloads

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._payloads:
            await asyncio.sleep(60)  # block "for a while" once exhausted, like a real idle subscription
        return FakeMsg(self._payloads.pop(0))


class FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeSubscription:
    def __init__(self, payloads: list[bytes]) -> None:
        self.messages = FakeJetStreamMessages(payloads)


class FakeJetStream:
    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = payloads

    async def subscribe(self, subject, *, stream, config):
        return FakeSubscription(self._payloads)


def envelope_bytes(*, event_id, event_type, aggregate_id, payload, occurred_at="2026-09-23T00:00:00Z"):
    return json.dumps({"event_id": event_id, "event_type": event_type, "aggregate_type": "x",
                       "aggregate_id": aggregate_id, "aggregate_version": 1, "occurred_at": occurred_at,
                       "payload": payload, "correlation_id": None, "causation_id": None}).encode("utf-8")


class NatsObservationSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_translates_a_real_trade_observation_event_honestly(self):
        hub = RealtimeHub()
        payload = envelope_bytes(event_id="evt-1", event_type="trade.observation.recorded.v1",
                                 aggregate_id="MT_1",
                                 payload={"observation_id": "TOBS_1", "managed_trade_id": "MT_1",
                                         "observation_seq": 3, "instrument": "XAUUSD"})
        js = FakeJetStream([payload])
        source = NatsObservationSource(hub)
        await source.start(js)
        await asyncio.sleep(0.05)  # let the consume task run

        self.assertEqual(hub.current_sequence(RESOURCE_TRADE_MANAGEMENT), 1)
        replay = hub.replay_since(RESOURCE_TRADE_MANAGEMENT, 0)
        self.assertEqual(replay[0]["type"], "trade_observation.created")
        self.assertEqual(replay[0]["eventId"], "evt-1")  # the REAL domain event id, not a fresh uuid
        self.assertEqual(replay[0]["payload"]["managedTradeId"], "MT_1")
        self.assertEqual(replay[0]["payload"]["observationSeq"], 3)


class NatsSignalSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_translates_a_real_signal_created_event_honestly(self):
        hub = RealtimeHub()
        payload = envelope_bytes(event_id="evt-2", event_type="signal.entry.created.v1",
                                 aggregate_id="SIG_1", payload={"signal_id": "SIG_1", "entry_signal_hash": "H"})
        js = FakeJetStream([payload])
        source = NatsSignalSource(hub)
        await source.start(js)
        await asyncio.sleep(0.05)

        replay = hub.replay_since(RESOURCE_SIGNALS, 0)
        self.assertEqual(replay[0]["type"], "signal.created")
        self.assertEqual(replay[0]["eventId"], "evt-2")
        self.assertEqual(replay[0]["resourceId"], "SIG_1")


class BoundedChangePollerTests(unittest.IsolatedAsyncioTestCase):
    def _query_fn(self, tables: dict[str, list[dict]]):
        def query(sql: str, params) -> list[dict]:
            # Check the most specific table names first: the decision/publication queries also
            # select a `managed_trade_id` column, so their SQL text contains "MANAGED_TRADE" too.
            upper = sql.upper()
            if "PUBLICATION_DECISION" in upper:
                return tables.get("publication", [])
            if "TRADE_MANAGER_DECISION" in upper:
                return tables.get("decision", [])
            if "FROM TRADE_MANAGEMENT.MANAGED_TRADE" in upper:
                return tables.get("managed_trade", [])
            if "ENTRY_SIGNALS" in upper:
                return tables.get("signals", [])
            if "RUNTIME_INSTANCES" in upper:
                return tables.get("orchestrator", [])
            return []
        return query

    async def test_new_managed_trade_row_becomes_a_managed_trade_created_event(self):
        hub = RealtimeHub()
        rows = {"managed_trade": [{"managed_trade_id": "MT_1", "entry_signal_id": "SIG_1",
                                   "instrument": "XAUUSD", "direction": "LONG", "state": "OPEN",
                                   "strategy_id": "S", "created_at": "2026-09-23T00:00:00Z"}]}
        poller = BoundedChangePoller(hub, self._query_fn(rows))
        await poller.tick()
        replay = hub.replay_since(RESOURCE_TRADE_MANAGEMENT, 0)
        types = [m["type"] for m in replay]
        self.assertIn("managed_trade.created", types)

    async def test_a_row_already_seen_is_never_re_emitted_on_the_next_tick(self):
        hub = RealtimeHub()
        rows = {"decision": [{"decision_id": "TMD_1", "managed_trade_id": "MT_1",
                              "observation_id": "TOBS_1", "action": "HOLD",
                              "persisted_at": "2026-09-23T00:00:00Z"}]}
        poller = BoundedChangePoller(hub, self._query_fn(rows))
        await poller.tick()
        first_count = hub.current_sequence(RESOURCE_TRADE_MANAGEMENT)
        rows["decision"] = []  # simulate the watermark query now returning nothing new
        await poller.tick()
        self.assertEqual(hub.current_sequence(RESOURCE_TRADE_MANAGEMENT), first_count)

    async def test_the_same_underlying_change_yields_the_same_derived_event_id_across_ticks(self):
        # Proves stable_event_id()'s determinism end to end: even if a poller somehow observed
        # the exact same row twice (e.g. a watermark boundary re-read), the eventId it would mint
        # is identical, so client-side dedup (section 7) still collapses it correctly.
        hub = RealtimeHub()
        row = {"decision_id": "TMD_1", "managed_trade_id": "MT_1", "observation_id": "TOBS_1",
              "action": "HOLD", "persisted_at": "2026-09-23T00:00:00Z"}
        poller_a = BoundedChangePoller(hub, self._query_fn({"decision": [row]}))
        await poller_a.tick()
        first = hub.replay_since(RESOURCE_TRADE_MANAGEMENT, 0)[0]["eventId"]

        hub2 = RealtimeHub()
        poller_b = BoundedChangePoller(hub2, self._query_fn({"decision": [row]}))
        await poller_b.tick()
        second = hub2.replay_since(RESOURCE_TRADE_MANAGEMENT, 0)[0]["eventId"]
        self.assertEqual(first, second)

    async def test_signal_outcome_change_is_detected_only_on_a_real_transition(self):
        hub = RealtimeHub()
        signals = [{"signal_id": "SIG_1", "terminal_state": "PENDING"}]
        query = self._query_fn({"signals": signals})
        poller = BoundedChangePoller(hub, query)
        await poller.tick()  # first observation: no prior state, so no "changed" event
        self.assertEqual(hub.current_sequence(RESOURCE_SIGNALS), 0)

        signals[0]["terminal_state"] = "ENTRY_ONLY"
        await poller.tick()  # now a real transition
        replay = hub.replay_since(RESOURCE_SIGNALS, 0)
        self.assertEqual(replay[0]["type"], "signal.outcome_changed")
        self.assertEqual(replay[0]["payload"]["terminalState"], "ENTRY_ONLY")


class SystemStatusPollerTests(unittest.IsolatedAsyncioTestCase):
    def _query_fn(self, running: list[int]):
        def query(sql: str, params) -> list[dict]:
            if "RUNTIME_INSTANCES" not in sql.upper():
                return []
            return [{"orchestrator_running": running.pop(0)}]
        return query

    async def test_first_observation_never_emits_no_prior_baseline(self):
        hub = RealtimeHub()
        poller = BoundedChangePoller(hub, self._query_fn([1]))
        await poller.tick()
        self.assertEqual(hub.current_sequence(RESOURCE_SYSTEM), 0)

    async def test_a_real_transition_emits_system_status_changed(self):
        hub = RealtimeHub()
        running = [1, 0]  # up, then down
        poller = BoundedChangePoller(hub, self._query_fn(running))
        await poller.tick()
        await poller.tick()
        replay = hub.replay_since(RESOURCE_SYSTEM, 0)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["type"], "system.status_changed")
        self.assertEqual(replay[0]["payload"]["running"], False)

    async def test_no_change_between_ticks_emits_nothing(self):
        hub = RealtimeHub()
        poller = BoundedChangePoller(hub, self._query_fn([1, 1, 1]))
        await poller.tick()
        await poller.tick()
        await poller.tick()
        self.assertEqual(hub.current_sequence(RESOURCE_SYSTEM), 0)


if __name__ == "__main__":
    unittest.main()
