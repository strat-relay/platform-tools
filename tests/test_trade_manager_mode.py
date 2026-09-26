"""Trade Manager operator mode (OFF | SHADOW, migration 024).

SHADOW is the existing advisory behaviour. OFF makes the runtime do no work: no ManagedTrade
creation (recorded as a TRADE_MANAGER_OFF skip), no observation tick, no decisions. An
unreadable/invalid mode fails the unit of work (rolled back for redelivery) instead of being
recorded as a permanent skip.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect

from platform_api.control import PlatformControlApi
from platform_api.trade_manager_mode import TradeManagerModeApi
from trade_management.binding import DefaultTmNoneResolver
from trade_management.decision_engine import record_decision
from trade_management.fakes import FakeConnection
from trade_management.managed_trade import create_managed_trade
from trade_management.market_data import FakeMarketDataProvider, MarketQuote
from trade_management.mode import (OFF, SHADOW, TradeManagerModeError, TradeManagerModeUnavailable,
                                   current_mode, read_mode_record, set_mode)
from trade_management.observation import record_observation
from trade_management.runtime.fakes import RuntimeFakeConnection
from trade_management.runtime.observation_runtime import observation_tick
from trade_management.versions import TM_NONE_1_MANIFEST

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()
ROOT = Path(__file__).resolve().parents[1]


def seed(conn, signal_id="SIG_1"):
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
    conn.seed_entry_signal(signal_id=signal_id, strategy_id="STRAT_A", strategy_version="V1",
                           strategy_ref="STRAT_A@V1", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="inst-1",
                           instrument="XAUUSD", direction="LONG", decision_time="2026-09-26T11:00:00Z",
                           entry_price=100.0, stop_price=99.0, risk_distance=1.0, target_price=103.0,
                           entry_signal_hash=f"HASH_{signal_id}")


def create(conn, signal_id="SIG_1", event_id="evt-1"):
    return create_managed_trade(conn, event_id=event_id, signal_id=signal_id,
                                resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_ID), now_utc=NOW)


def quote(bid=101.0, ts="2026-09-26T12:00:00.000Z"):
    return MarketQuote(instrument="XAUUSD", bid=bid, ask=bid + 0.2, source_timestamp=ts, provider_id="fake")


class RecordingPublisher:
    def __init__(self):
        self.published = []

    async def publish(self, envelope):
        self.published.append(envelope)


class RuntimeGateTests(unittest.IsolatedAsyncioTestCase):
    def test_unseeded_mode_is_the_migration_seed_shadow(self):
        self.assertEqual(current_mode(FakeConnection()), SHADOW)

    def test_shadow_creates_managed_trades_as_before(self):
        conn = FakeConnection()
        seed(conn)
        self.assertEqual(create(conn).status, "CREATED")

    def test_off_skips_creation_and_records_why(self):
        conn = FakeConnection()
        seed(conn)
        conn.set_tm_mode(OFF)
        result = create(conn)
        self.assertEqual((result.status, result.reason), ("SKIPPED", "TRADE_MANAGER_OFF"))
        self.assertEqual(conn.tables["trade_management.managed_trade"], {})
        self.assertEqual(conn.tables["trade_management.managed_trade_skip"]["SIG_1"]["reason"], "TRADE_MANAGER_OFF")
        self.assertEqual(conn.tables["platform.inbox_events"][("trade-mgmt-open", "evt-1")]["status"], "PROCESSED")
        self.assertFalse(any(k.startswith("SIG_1") or "trade.opened" in str(k)
                             for k in conn.tables.get("platform.outbox_events", {})))

    def test_unreadable_mode_rolls_back_instead_of_skipping(self):
        conn = FakeConnection()
        seed(conn)
        conn.set_tm_mode("BOGUS")
        with self.assertRaises(TradeManagerModeUnavailable):
            create(conn)
        self.assertEqual(conn.tables.get("platform.inbox_events", {}), {})  # claim rolled back -> redelivered
        self.assertEqual(conn.tables.get("trade_management.managed_trade_skip", {}), {})
        conn.set_tm_mode(SHADOW, revision=2)
        self.assertEqual(create(conn).status, "CREATED")

    def test_off_records_no_decision(self):
        conn = FakeConnection()
        seed(conn)
        trade_id = create(conn).managed_trade_id
        obs = record_observation(conn, managed_trade_id=trade_id, quote=quote(), bars=None, now_utc=NOW)
        conn.set_tm_mode(OFF)
        result = record_decision(conn, observation_id=obs.observation_id, event_id=obs.observation_id, now_utc=NOW)
        self.assertEqual(result.status, "TRADE_MANAGER_OFF")
        self.assertEqual(conn.tables.get("trade_management.trade_manager_decision", {}), {})

    async def test_off_skips_the_observation_tick_and_shadow_resumes(self):
        conn = RuntimeFakeConnection()
        seed(conn)
        trade_id = create(conn).managed_trade_id
        provider = FakeMarketDataProvider()
        provider.set_quote("XAUUSD", quote())
        publisher = RecordingPublisher()

        conn.set_tm_mode(OFF)
        summary = await observation_tick(conn, publisher, provider)
        self.assertEqual(summary, {"mode": OFF, "skipped": True})
        self.assertEqual(provider.quote_calls, 0)
        self.assertEqual(conn.tables.get("trade_management.trade_observation", {}), {})

        conn.set_tm_mode(SHADOW, revision=2)
        summary = await observation_tick(conn, publisher, provider)
        self.assertEqual((summary["mode"], summary["observations_recorded"]), (SHADOW, 1))
        self.assertEqual(next(iter(conn.tables["trade_management.trade_observation"].values()))["managed_trade_id"],
                         trade_id)

    async def test_unreadable_mode_skips_the_tick(self):
        conn = RuntimeFakeConnection()
        seed(conn)
        conn.set_tm_mode("BOGUS")
        provider = FakeMarketDataProvider()
        summary = await observation_tick(conn, RecordingPublisher(), provider)
        self.assertEqual((summary["mode"], summary["skipped"]), ("UNAVAILABLE", True))
        self.assertEqual(provider.quote_calls, 0)

    def test_lenient_read_reports_invalid_as_off(self):
        conn = FakeConnection()
        conn.set_tm_mode("BOGUS")
        self.assertEqual(read_mode_record(conn)["mode"], OFF)
        self.assertEqual(read_mode_record(conn)["error"], "MODE_INVALID")

    def test_no_broker_effect_mode_exists(self):
        with self.assertRaises(TradeManagerModeError):
            set_mode(FakeConnection(), "ACTIVE", expected_revision=1, changed_by="test")
        sql = (ROOT / "postgres" / "migrations" / "024_trade_manager_mode_control.sql").read_text()
        self.assertIn("CHECK (mode IN ('OFF', 'SHADOW'))", sql)
        self.assertIn("VALUES ('current', 'SHADOW', 1, 'migration:024')", sql)


class FakeModeApi:
    def __init__(self):
        self.saved = []

    def read(self):
        return 200, {"data": {"mode": SHADOW, "revision": 3}}

    def save(self, body):
        self.saved.append(json.loads(body))
        return 200, {"data": {"mode": OFF, "revision": 4}}


class RouteTests(unittest.TestCase):
    def api(self, mode_api):
        class Repo:
            def execution_runtime_status(self):
                return {}
        return PlatformControlApi(repository=Repo(), environ={}, bridge_reader=object(), v2_risk_api=object(),
                                  trade_manager_mode_api=mode_api)

    def test_get_and_post_route_to_the_mode_api(self):
        mode_api = FakeModeApi()
        api = self.api(mode_api)
        self.assertEqual(api.execute("GET", "/api/v1/trade-manager/mode")[1]["data"]["mode"], SHADOW)
        status, body = api.execute("POST", "/api/v1/trade-manager/mode",
                                   json.dumps({"mode": OFF, "expectedRevision": 3}).encode())
        self.assertEqual((status, body["data"]["mode"]), (200, OFF))
        self.assertEqual(mode_api.saved, [{"mode": OFF, "expectedRevision": 3}])

    def test_invalid_requests_are_rejected_before_any_write(self):
        api = TradeManagerModeApi(connect_fn=lambda **_: (_ for _ in ()).throw(AssertionError("no DB expected")))
        for body in (b"", b"not json", json.dumps({"mode": "ACTIVE", "expectedRevision": 1}).encode(),
                     json.dumps({"mode": OFF}).encode(), json.dumps({"mode": OFF, "expectedRevision": True}).encode()):
            status, payload = api.save(body)
            self.assertEqual((status, payload["error"]), (400, "INVALID_REQUEST"), body)

    def test_unreachable_database_reads_as_off_with_error(self):
        def broken(**_):
            raise RuntimeError("db down")
        status, body = TradeManagerModeApi(connect_fn=broken).read()
        self.assertEqual((status, body["data"]["mode"], body["data"]["error"]), (200, OFF, "MODE_UNAVAILABLE"))
        self.assertTrue(body["degraded"])

    def test_cors_preflight_allows_post_on_the_mode_path(self):
        source = (ROOT / "platform_api" / "signals.py").read_text()
        allowlist = source[source.index("_POST_ALLOWED_PATHS"):source.index("def do_OPTIONS")]
        self.assertIn('"/api/v1/trade-manager/mode"', allowlist)


def _database_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class RealPostgresModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _connect(self, readonly=False):
        return connect(PostgresConfig.from_env(), readonly=readonly)

    def test_api_round_trip_is_revision_checked_and_audited(self):
        api = TradeManagerModeApi(connect_fn=self._connect)
        _, before = api.read()
        start = before["data"]
        self.assertIn(start["mode"], (OFF, SHADOW))
        target = OFF if start["mode"] == SHADOW else SHADOW

        status, body = api.save(json.dumps({"mode": target, "expectedRevision": start["revision"],
                                            "updatedBy": "test", "reason": "unit test"}).encode())
        self.assertEqual((status, body["data"]["mode"]), (200, target))
        stale_status, stale = api.save(json.dumps({"mode": start["mode"], "expectedRevision": start["revision"]}).encode())
        self.assertEqual((stale_status, stale["error"]), (409, "MODE_CONFLICT"))

        with self.conn.cursor() as cur:
            cur.execute("""SELECT previous_mode, new_mode, changed_by, reason FROM trade_management.trade_manager_mode_change
                          WHERE new_revision = %s""", (start["revision"] + 1,))
            self.assertEqual(cur.fetchone(), (start["mode"], target, "test", "unit test"))
        self.conn.rollback()
        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM trade_management.trade_manager_mode_change")
        self.conn.rollback()
        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE trade_management.trade_manager_mode SET mode = 'ACTIVE'")
        self.conn.rollback()

        # Restore to SHADOW so other real-DB suites see the seeded behaviour.
        _, now = api.read()
        if now["data"]["mode"] != SHADOW:
            api.save(json.dumps({"mode": SHADOW, "expectedRevision": now["data"]["revision"]}).encode())
        self.assertEqual(current_mode(self.conn), SHADOW)
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
