"""Redis-first broker view (broker_view/): populator + reader + API wiring.

Proves: the populator stores each allow-listed read once per pass (history-orders and deals share
one read) and never calls anything else; the API serves a fresh Redis entry without touching the
bridge and reports its source and observed_at; a missing or stale entry, or a dead Redis, falls
back to the bridge; a bridge transport failure skips the rest of the pass and keeps older entries;
the Trade Manager live projection reads through the view and reports the oldest component time.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import fakeredis

from broker_view.reader import SOURCE_BRIDGE, SOURCE_REDIS, RedisFirstBridgeReader
from broker_view.service import populate_once, unique_reads
from broker_view.store import BrokerViewStore
from platform_api.control import READ_ONLY_BROKER_TOOLS, PlatformControlApi
from test_platform_control_api import FakeRepository as ControlRepository
from test_trade_manager_live_projection import (ACCOUNT, FakeRepository, RecordingBridge, READ_TOOLS, linked_row,
                                                position)
from platform_api.trade_manager_live import TradeManagerLiveProjection

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Bridge:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def call(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        if tool not in READ_TOOLS:
            raise AssertionError(f"non-read tool {tool}")
        if self.error:
            raise self.error
        return {"tool": tool, "n": len(self.calls)}


class DeadRedis:
    def get(self, key):
        raise ConnectionError("redis down")


def reader(store, bridge, clock, max_age=45.0):
    return RedisFirstBridgeReader(store, bridge, allowed_tools=READ_TOOLS, max_age_seconds=max_age, clock=clock)


class PopulatorTests(unittest.TestCase):
    def setUp(self):
        self.store = BrokerViewStore(fakeredis.FakeRedis())
        self.reads = unique_reads(READ_ONLY_BROKER_TOOLS)

    def test_each_allow_listed_read_once_per_pass(self):
        self.assertEqual([t for t, _ in self.reads],
                         ["mt5_account_info", "mt5_positions", "mt5_orders", "mt5_history", "mt5_symbols"])
        bridge = Bridge()
        health = populate_once(bridge, self.store, self.reads, clock=Clock())
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(bridge.calls, self.reads)
        entry = self.store.get("mt5_history", {"limit": 500})
        self.assertEqual((entry.observed_at, entry.data["tool"]), (T0, "mt5_history"))
        self.assertEqual(self.store.health()["ok"], [t for t, _ in self.reads])

    def test_tool_error_is_isolated_and_transport_failure_ends_the_pass(self):
        populate_once(Bridge(), self.store, self.reads, clock=Clock())

        class OneBad(Bridge):
            def call(self, tool, arguments=None):
                if tool == "mt5_positions":
                    self.calls.append((tool, arguments))
                    raise RuntimeError("tool failed")
                return super().call(tool, arguments)
        bad = OneBad()
        health = populate_once(bad, self.store, self.reads, clock=Clock(T0 + 15))
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(len(bad.calls), 5)
        self.assertEqual(self.store.get("mt5_positions", None).observed_at, T0)      # kept, not erased

        down = Bridge(error=TimeoutError("timed out"))
        health = populate_once(down, self.store, self.reads, clock=Clock(T0 + 30))
        self.assertEqual(health["status"], "unavailable")
        self.assertEqual(len(down.calls), 1)                                       # no pile-up of timeouts
        self.assertIn("skipped", health["errors"]["mt5_symbols"])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.store = BrokerViewStore(fakeredis.FakeRedis())
        self.clock = Clock()
        populate_once(Bridge(), self.store, unique_reads(READ_ONLY_BROKER_TOOLS), clock=self.clock)

    def test_fresh_entry_is_served_without_the_bridge(self):
        bridge = Bridge()
        self.clock.now = T0 + 44
        data, observed_at, source = reader(self.store, bridge, self.clock).read("mt5_account_info")
        self.assertEqual((data["tool"], observed_at, source), ("mt5_account_info", T0, SOURCE_REDIS))
        self.assertEqual(bridge.calls, [])

    def test_stale_or_missing_entry_or_dead_redis_falls_back_to_the_bridge(self):
        for store, now in ((self.store, T0 + 46), (BrokerViewStore(fakeredis.FakeRedis()), T0),
                           (BrokerViewStore(DeadRedis()), T0)):
            bridge, self.clock.now = Bridge(), now
            with self.subTest(now=now):
                _, observed_at, source = reader(store, bridge, self.clock).read("mt5_positions")
                self.assertEqual((source, observed_at), (SOURCE_BRIDGE, now))
                self.assertEqual(bridge.calls, [("mt5_positions", None)])

    def test_non_read_tools_are_refused_even_if_cached(self):
        self.store.put("mt5_order_send", None, {"x": 1}, observed_at=T0)
        with self.assertRaises(ValueError):
            reader(self.store, Bridge(), self.clock).call("mt5_order_send")


class ApiTests(unittest.TestCase):
    def test_broker_routes_report_redis_source_and_observed_at(self):
        store, clock = BrokerViewStore(fakeredis.FakeRedis()), Clock()
        populate_once(Bridge(), store, unique_reads(READ_ONLY_BROKER_TOOLS), clock=clock)
        bridge = Bridge()
        api = PlatformControlApi(repository=ControlRepository(), environ={},
                                 bridge_reader=reader(store, bridge, Clock(T0 + 10)))
        for path, tool in (("/api/v1/broker/account", "mt5_account_info"), ("/api/v1/broker/deals", "mt5_history"),
                           ("/api/v1/exposure", "mt5_positions")):
            with self.subTest(path=path):
                status, body = api.execute("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual((body["source"], body["data"]["tool"]), (SOURCE_REDIS, tool))
                self.assertEqual(body["observed_at"], datetime.fromtimestamp(T0, timezone.utc).isoformat())
        self.assertEqual(bridge.calls, [])

    def test_env_wires_the_redis_first_reader(self):
        api = PlatformControlApi(repository=ControlRepository(),
                                 environ={"BROKER_VIEW_REDIS_URL": "redis://127.0.0.1:1/0"})
        self.assertIsInstance(api.bridge_reader, RedisFirstBridgeReader)
        self.assertIsNotNone(PlatformControlApi(repository=ControlRepository(), environ={}).bridge_reader)
        self.assertNotIsInstance(PlatformControlApi(repository=ControlRepository(), environ={}).bridge_reader,
                                 RedisFirstBridgeReader)


class TradeManagerLiveTests(unittest.TestCase):
    def test_projection_reads_the_view_and_reports_the_oldest_component(self):
        store = BrokerViewStore(fakeredis.FakeRedis())
        source = RecordingBridge(positions=[position()])
        store.put("mt5_account_info", None, source.call("mt5_account_info"), observed_at=T0 - 20)
        store.put("mt5_positions", None, source.call("mt5_positions"), observed_at=T0 - 5)
        store.put("mt5_orders", None, source.call("mt5_orders"), observed_at=T0 - 10)
        bridge = RecordingBridge(fail=True)
        now = datetime.fromtimestamp(T0, timezone.utc)
        result = TradeManagerLiveProjection(FakeRepository(linked=[linked_row()]), reader(store, bridge, Clock()),
                                            clock=lambda: now).project()
        self.assertEqual(bridge.calls, [])
        self.assertEqual(result["broker"]["status"], "CONNECTED")
        self.assertEqual(result["broker"]["account_id"], ACCOUNT)
        self.assertEqual(result["broker"]["observed_at"],
                         datetime.fromtimestamp(T0 - 20, timezone.utc).isoformat().replace("+00:00", "Z"))


if __name__ == "__main__":
    unittest.main()
