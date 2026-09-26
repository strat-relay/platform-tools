"""Strategy instances as operational units: instance-scoped stats and revision-checked ONLINE/OFFLINE.

Proves per-instance statistics are scoped by (strategy_id, strategy_instance_id) and never
aggregated across a family, that ONLINE/OFFLINE writes only platform.strategy_instance and never
touches execution authority / V2 risk policy / allow-lists, that it is refused where the runtime
does not enforce it (Context today), and that the orchestrator honours it every cycle.
"""
from __future__ import annotations

import json
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from orchestration.config import load_instances_from_database, refresh_instances  # noqa: E402
from platform_api.control import PlatformControlApi  # noqa: E402
from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import apply_migrations, connect  # noqa: E402
from signal_orchestrator import load_adapters  # noqa: E402

CONTEXT = "CONTEXT_STRUCTURE_RETRACE_V1"
LIQUIDITY = "LIQUIDITY_DISPLACEMENT_SCALP_V1"
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


class OrchestratorLifecycleTests(unittest.TestCase):
    """No database: load_adapters and refresh_instances on plain config dicts."""

    PARENT = {"strategy_id": LIQUIDITY, "enabled": True}

    def instances(self, **enabled):
        return [{"instance_id": iid, "strategy_id": LIQUIDITY, "enabled": on} for iid, on in enabled.items()]

    def test_all_instances_offline_produces_no_liquidity_adapter(self):
        config = {"strategies": [self.PARENT],
                  "instances": self.instances(**{"liquidity-xau33": False, "liquidity-btc25": False})}
        self.assertEqual(load_adapters(config, "2026-09-26T00:00:00+00:00"), [])

    def test_online_instance_gets_its_own_adapter(self):
        config = {"strategies": [self.PARENT],
                  "instances": self.instances(**{"liquidity-xau33": True, "liquidity-btc25": False})}
        self.assertEqual([a.definition.instance_id for a in load_adapters(config, "2026-09-26T00:00:00+00:00")],
                         ["liquidity-xau33"])

    def test_parent_only_fallback_needs_no_instance_rows_at_all(self):
        adapters = load_adapters({"strategies": [self.PARENT], "instances": []}, "2026-09-26T00:00:00+00:00")
        self.assertEqual([type(a).__name__ for a in adapters], ["LiquidityDisplacementAdapter"])

    def test_refresh_replaces_only_instances_and_keeps_previous_on_failure(self):
        base = {"mcp_url": "x", "instances": self.instances(**{"liquidity-xau33": False})}

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return Cursor()

        class Cursor(Conn):
            def execute(self, *_):
                pass

            def fetchall(self):
                return [("liquidity-xau33", LIQUIDITY, "XAU 33%", True, {"symbol": "XAUUSDm"})]

        refreshed = refresh_instances(base, connect_fn=Conn)
        self.assertEqual((refreshed["mcp_url"], refreshed["instances"][0]["enabled"]), ("x", True))

        def broken():
            raise RuntimeError("db down")
        self.assertIs(refresh_instances(base, connect_fn=broken), base)


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class StrategyInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_integration_tm_membership_runner import FreshDatabase
        from platform_api.strategy_catalog import StrategyCatalogRepository
        cls.db = FreshDatabase()
        with cls.db.connect() as conn:
            apply_migrations(conn)
            conn.commit()
        cls.repo = StrategyCatalogRepository(connect_fn=lambda readonly=False: cls.db.connect(readonly=readonly))

    @classmethod
    def tearDownClass(cls):
        cls.db.drop()

    def setUp(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM strategy.entry_signal_outcomes")
            cur.execute("DELETE FROM strategy.entry_signals")
            cur.execute("DELETE FROM platform.strategy_instance WHERE strategy_id = %s", (LIQUIDITY,))
            cur.execute("DELETE FROM platform.strategy_definition WHERE strategy_id = %s", (LIQUIDITY,))
            cur.execute("""INSERT INTO platform.strategy_definition
                (strategy_id, strategy_version, display_name, adapter, enabled, routes, updated_by)
                VALUES (%s, 'V1', 'Liquidity Displacement Scalp V1', 'LiquidityDisplacementAdapter', false,
                        '{}'::jsonb, 'test')""", (LIQUIDITY,))
            for iid, name, attrs in (
                    ("liquidity-xau33", "Liquidity Displacement Scalp — XAU 33%",
                     {"symbol": "XAUUSDm", "target_r": 1.25, "entry_fraction": 0.3333, "max_hold_minutes": 120,
                      "parent_strategy_id": LIQUIDITY, "provider_symbol": "XAUUSDm"}),
                    ("liquidity-btc25", "Liquidity Displacement Scalp — BTC 25%",
                     {"symbol": "BTCUSDm", "target_r": 1.25, "entry_fraction": 0.25, "max_hold_minutes": 120})):
                cur.execute("""INSERT INTO platform.strategy_instance
                    (instance_id, strategy_id, display_name, enabled, attributes, updated_by)
                    VALUES (%s, %s, %s, false, %s::jsonb, 'test')""", (iid, LIQUIDITY, name, json.dumps(attrs)))
            # Context phase6: 5 signals (2 OPEN, 1 STOPPED); 2 more with no instance id (legacy).
            self._signals(cur, CONTEXT, "phase6", ["OPEN", "OPEN", "STOPPED", None, None], "EURUSD")
            self._signals(cur, CONTEXT, None, [None, None], "EURUSD")
            # Liquidity: xau33 3 signals (1 OPEN, 1 TIME_EXIT), btc25 1 TARGET_HIT.
            self._signals(cur, LIQUIDITY, "liquidity-xau33", ["OPEN", "TIME_EXIT", None], "XAUUSD", hours=100)
            self._signals(cur, LIQUIDITY, "liquidity-btc25", ["TARGET_HIT"], "BTCUSD", hours=200)
            conn.commit()

    @staticmethod
    def _signals(cur, strategy, instance, statuses, instrument, hours=0):
        for n, status in enumerate(statuses):
            tag = uuid.uuid4().hex[:10]
            when = T0 + timedelta(hours=hours + n)
            cur.execute("""INSERT INTO strategy.evaluations (evaluation_id, strategy_id, instrument, decision_time,
                decision, trace_fidelity, runtime_version, evaluator_version, canonical_payload, canonical_hash)
                VALUES (%s,%s,%s,%s,'SIGNAL','L1','t','t','{}'::jsonb,%s)""", (f"E{tag}", strategy, instrument, when,
                                                                             f"H{tag}"))
            cur.execute("""INSERT INTO strategy.entry_signals (signal_id, candidate_id, evaluation_id, strategy_ref,
                strategy_id, strategy_version, strategy_instance_id, instrument, direction, decision_time,
                signal_emitted_at, entry_price, stop_price, target_price, target_r, evaluation_hash, trace_hash,
                terminal_state, entry_signal_hash)
                VALUES (%s,%s,%s,%s,%s,'V1',%s,%s,'LONG',%s,%s,1.1,1.09,1.105,0.5,'e','t','ENTRY_SIGNAL_CREATED',%s)""",
                        (f"SIG{tag}", f"C{tag}", f"E{tag}", f"{strategy}@V1", strategy, instance, instrument, when,
                         when + timedelta(minutes=5), f"h{tag}"))
            if status is None:
                continue
            closed = status != "OPEN"
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes (signal_id, outcome_type, status, realized_r,
                exit_timestamp, source) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (f"SIG{tag}", "ENTRY_ONLY" if strategy == CONTEXT else "LIQUIDITY_ENTRY", status,
                         (0.5 if status == "TARGET_HIT" else -1.0) if closed else None,
                         when + timedelta(minutes=30) if closed else None, strategy))

    def _by_id(self):
        return {i["instance_id"]: i for s in self.repo.list_strategies() for i in s["instances"]}

    @contextmanager
    def _unchanged(self, *tables):
        def snapshot():
            with self.db.connect(readonly=True) as conn, conn.cursor() as cur:
                out = {}
                for table in tables:
                    cur.execute(f"SELECT to_jsonb(t) FROM {table} t ORDER BY 1::text")
                    out[table] = sorted(json.dumps(r[0], sort_keys=True, default=str) for r in cur.fetchall())
                return out
        before = snapshot()
        yield
        self.assertEqual(snapshot(), before)

    # --- stats -----------------------------------------------------------------

    def test_stats_are_scoped_by_instance_not_aggregated_across_the_family(self):
        by_id = self._by_id()
        stats = {iid: (i["stats"]["signals"], i["stats"]["open"], i["stats"]["closed"]) for iid, i in by_id.items()}
        self.assertEqual(stats, {"phase6": (5, 2, 1), "liquidity-xau33": (3, 1, 1), "liquidity-btc25": (1, 0, 1)})
        self.assertEqual(by_id["liquidity-xau33"]["stats"]["outcomes_untracked"], 1)
        self.assertEqual(by_id["liquidity-xau33"]["stats"]["scope"], "STRATEGY_INSTANCE")
        self.assertEqual(by_id["liquidity-btc25"]["stats"]["last_event"]["type"], "TARGET_HIT")

    def test_signals_without_an_instance_stay_visible_as_unattributed(self):
        [context] = [s for s in self.repo.list_strategies() if s["strategy_id"] == CONTEXT]
        self.assertEqual((context["signals_published"], context["unattributed_signals"], context["instance_count"]),
                         (7, 2, 1))

    def test_instance_with_no_history_reports_no_last_event(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM strategy.entry_signal_outcomes WHERE signal_id IN (SELECT signal_id
                           FROM strategy.entry_signals WHERE strategy_instance_id = 'liquidity-btc25')""")
            cur.execute("DELETE FROM strategy.entry_signals WHERE strategy_instance_id = 'liquidity-btc25'")
            conn.commit()
        btc = self._by_id()["liquidity-btc25"]
        self.assertEqual((btc["stats"]["signals"], btc["stats"]["last_event_at"], btc["stats"]["last_event"]),
                         (0, None, None))

    def test_liquidity_instances_are_offline_and_context_membership_is_carried(self):
        by_id = self._by_id()
        self.assertEqual({iid: i["lifecycle_state"] for iid, i in by_id.items() if iid.startswith("liquidity")},
                         {"liquidity-xau33": "OFFLINE", "liquidity-btc25": "OFFLINE"})
        self.assertEqual(by_id["phase6"]["lifecycle_state"], "ONLINE")
        self.assertEqual(by_id["phase6"]["instruments"]["active"], ["BTCUSD", "EURUSD", "USDJPY", "XAUUSD"])
        self.assertEqual(by_id["liquidity-xau33"]["instruments"]["active_count"], 0)   # no membership rows
        self.assertEqual([p["key"] for p in by_id["liquidity-xau33"]["parameters"]],
                         ["symbol", "target_r", "entry_fraction", "max_hold_minutes"])
        self.assertEqual((by_id["liquidity-xau33"]["lifecycle_control"]["enforced"],
                          by_id["phase6"]["lifecycle_control"]["enforced"]), (True, False))
        self.assertEqual(by_id["liquidity-xau33"]["runtime"], {"parent_strategy_enabled": False, "effective": False})

    def test_execution_eligibility_is_reported_per_strategy_ref(self):
        xau = self._by_id()["liquidity-xau33"]["execution"]
        self.assertEqual(xau["strategy_ref"], f"{LIQUIDITY}@V1")
        self.assertFalse(xau["eligible"])
        self.assertFalse(xau["allow_listed"])

    def test_instance_page_lists_only_this_instance(self):
        page = self.repo.instance_page(LIQUIDITY, "liquidity-xau33")
        self.assertEqual({o["symbol"] for o in page["observations"]}, {"XAUUSD"})
        self.assertEqual(len(page["observations"]), 3)
        self.assertEqual(page["observations"][0]["status"], "OPEN")
        self.assertTrue(all(e["symbol"] == "XAUUSD" for e in page["events"]))
        self.assertEqual(page["strategy_display_name"], "Liquidity Displacement Scalp V1")
        self.assertIsNone(self.repo.instance_page(CONTEXT, "liquidity-xau33"))   # wrong parent
        self.assertIsNone(self.repo.instance_page(LIQUIDITY, "nope"))

    # --- ONLINE / OFFLINE --------------------------------------------------------

    EXECUTION_TABLES = ("execution_v2.execution_authority", "execution_v2.risk_policy",
                        "execution_v2.risk_policy_allowed_strategy", "execution_v2.risk_policy_allowed_account",
                        "execution_v2.risk_policy_allowed_symbol", "platform.strategy_definition")

    def test_online_then_offline_is_revision_checked_and_returns_database_state(self):
        from platform_api.strategy_catalog import InstanceRevisionConflict
        with self._unchanged(*self.EXECUTION_TABLES):
            online = self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-xau33", "ONLINE",
                                                      expected_revision=1, updated_by="operator@test")
            self.assertEqual((online["lifecycle_state"], online["revision"], online["updated_by"]),
                             ("ONLINE", 2, "operator@test"))
            with self.assertRaises(InstanceRevisionConflict):
                self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-xau33", "OFFLINE",
                                                 expected_revision=1, updated_by="operator@test")
            self.assertEqual(self._by_id()["liquidity-xau33"]["lifecycle_state"], "ONLINE")
            offline = self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-xau33", "OFFLINE",
                                                       expected_revision=2, updated_by="operator@test")
            self.assertEqual((offline["lifecycle_state"], offline["revision"]), ("OFFLINE", 3))
        self.assertEqual(self._by_id()["liquidity-btc25"]["revision"], 1)   # siblings untouched

    def test_toggle_does_not_change_execution_eligibility(self):
        before = self._by_id()["liquidity-xau33"]["execution"]
        after = self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-xau33", "ONLINE", expected_revision=1,
                                                 updated_by="t")["execution"]
        self.assertEqual(after, before)

    def test_context_lifecycle_is_refused_because_its_runtime_does_not_enforce_it(self):
        from platform_api.strategy_catalog import LifecycleNotEnforced
        with self._unchanged("platform.strategy_instance", *self.EXECUTION_TABLES):
            with self.assertRaises(LifecycleNotEnforced):
                self.repo.set_instance_lifecycle(CONTEXT, "phase6", "OFFLINE", expected_revision=1, updated_by="t")

    def test_invalid_requests_write_nothing(self):
        from platform_api.strategy_catalog import InstanceNotFound
        with self._unchanged("platform.strategy_instance"):
            for state, rev in (("PAUSED", 1), ("ONLINE", None), ("ONLINE", True), ("ONLINE", "1")):
                with self.assertRaises(ValueError):
                    self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-xau33", state, expected_revision=rev,
                                                     updated_by="t")
            with self.assertRaises(InstanceNotFound):
                self.repo.set_instance_lifecycle(CONTEXT, "liquidity-xau33", "ONLINE", expected_revision=1,
                                                 updated_by="t")

    def test_orchestrator_refresh_sees_the_toggle(self):
        with self.db.connect(readonly=True) as conn:
            instances = load_instances_from_database(conn)
        config = {"strategies": [{"strategy_id": LIQUIDITY, "enabled": True}], "instances": instances}
        self.assertEqual(load_adapters(config, "2026-09-26T00:00:00+00:00"), [])
        self.repo.set_instance_lifecycle(LIQUIDITY, "liquidity-btc25", "ONLINE", expected_revision=1, updated_by="t")
        config = refresh_instances(config, connect_fn=lambda: self.db.connect(readonly=True))
        self.assertEqual([a.definition.instance_id for a in load_adapters(config, "2026-09-26T00:00:00+00:00")],
                         ["liquidity-btc25"])

    # --- HTTP routes --------------------------------------------------------------

    def _api(self):
        class Repo:
            def execution_runtime_status(self):
                return {}
        api = PlatformControlApi(repository=Repo(), environ={}, bridge_reader=object(), v2_risk_api=object(),
                                 trade_manager_mode_api=object())
        api.strategy_catalog = self.repo
        return api

    def test_routes(self):
        api = self._api()
        path = f"/api/v1/strategies/{LIQUIDITY}/instances/liquidity-xau33"
        status, body = api.execute("GET", path)
        self.assertEqual((status, body["data"]["instance_id"]), (200, "liquidity-xau33"))
        self.assertEqual(api.execute("GET", f"/api/v1/strategies/{CONTEXT}/instances/liquidity-xau33")[0], 404)
        status, body = api.execute("POST", path + "/lifecycle",
                                   json.dumps({"state": "ONLINE", "expectedRevision": 1, "updatedBy": "op"}).encode())
        self.assertEqual((status, body["data"]["lifecycle_state"], body["data"]["revision"]), (200, "ONLINE", 2))
        status, body = api.execute("POST", path + "/lifecycle",
                                   json.dumps({"state": "OFFLINE", "expectedRevision": 1}).encode())
        self.assertEqual((status, body["error"]), (409, "REVISION_CONFLICT"))
        status, body = api.execute("POST", f"/api/v1/strategies/{CONTEXT}/instances/phase6/lifecycle",
                                   json.dumps({"state": "OFFLINE", "expectedRevision": 1}).encode())
        self.assertEqual((status, body["error"]), (409, "LIFECYCLE_NOT_ENFORCED"))
        self.assertEqual(api.execute("POST", path + "/lifecycle", b"nope")[0], 400)
        self.assertEqual(api.execute("GET", path + "/lifecycle")[0], 405)


if __name__ == "__main__":
    unittest.main()
