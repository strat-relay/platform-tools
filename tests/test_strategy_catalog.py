"""Strategy catalog + complete page model from PostgreSQL (migration 028).

Proves the strategy endpoint's stats cover the full canonical history (not a page of signals),
that outcome coverage is explicit (untracked is its own bucket), that the effective TM policy is
reported even without explicit bindings, and that strategies no longer come from platform.json.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import apply_migrations, connect  # noqa: E402

STRATEGY = "CONTEXT_STRUCTURE_RETRACE_V1"
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class StrategyPageModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_integration_tm_membership_runner import FreshDatabase
        from platform_api.strategy_catalog import StrategyCatalogRepository
        cls.db = FreshDatabase()
        with cls.db.connect() as conn:
            apply_migrations(conn)
            cls._seed(conn)
            conn.commit()
        cls.repo = StrategyCatalogRepository(connect_fn=lambda readonly=False: cls.db.connect(readonly=readonly))

    @classmethod
    def tearDownClass(cls):
        cls.db.drop()

    @classmethod
    def _seed(cls, conn):
        """130 signals (more than any signals page): 60 TARGET_HIT (+0.5R), 40 STOPPED (-1R),
        10 OPEN, 20 with no outcome row (pre-tracking)."""
        with conn.cursor() as cur:
            for i in range(130):
                tag = f"{i:03d}{uuid.uuid4().hex[:6]}"
                when = T0 + timedelta(hours=i)
                instrument = ("EURUSD", "XAUUSD")[i % 2]
                cur.execute("""INSERT INTO strategy.evaluations (evaluation_id, strategy_id, instrument, decision_time,
                    decision, trace_fidelity, runtime_version, evaluator_version, canonical_payload, canonical_hash)
                    VALUES (%s,%s,%s,%s,'SIGNAL','L1','t','t','{}'::jsonb,%s)""",
                            (f"E{tag}", STRATEGY, instrument, when, f"H{tag}"))
                cur.execute("""INSERT INTO strategy.entry_signals (signal_id, candidate_id, evaluation_id, strategy_ref,
                    strategy_id, strategy_version, instrument, direction, decision_time, signal_emitted_at, entry_price,
                    stop_price, target_price, target_r, source_provenance, evaluation_hash, trace_hash, terminal_state,
                    entry_signal_hash)
                    VALUES (%s,%s,%s,%s,%s,'V1',%s,'LONG',%s,%s,1.1,1.09,1.105,0.5,
                            '{"source_strategy_fingerprint":"0a990dd5","source_config_hash":"1f1da2a6","source_process":"context_structure_retrace_forward.py"}'::jsonb,
                            'e','t','ENTRY_SIGNAL_CREATED',%s)""",
                            (f"SIG{tag}", f"C{tag}", f"E{tag}", f"{STRATEGY}@V1", STRATEGY, instrument, when,
                             when + timedelta(minutes=5), f"h{tag}"))
                if i < 60:
                    status, r = "TARGET_HIT", 0.5
                elif i < 100:
                    status, r = "STOPPED", -1.0
                elif i < 110:
                    status, r = "OPEN", None
                else:
                    continue
                cur.execute("""INSERT INTO strategy.entry_signal_outcomes (signal_id, outcome_type, status, realized_r,
                    exit_timestamp, source) VALUES (%s,'ENTRY_ONLY',%s,%s,%s,%s)""",
                            (f"SIG{tag}", status, r, None if r is None else when + timedelta(minutes=30), STRATEGY))

    def test_list_stats_cover_full_history_not_a_page(self):
        [row] = [s for s in self.repo.list_strategies() if s["strategy_id"] == STRATEGY]
        self.assertEqual(row["signals_published"], 130)
        out = row["outcomes"]
        self.assertEqual((out["target_hits"], out["stops"], out["open"], out["closed"], out["tracked"], out["untracked"]),
                         (60, 40, 10, 100, 110, 20))
        self.assertAlmostEqual(out["win_rate"], 0.6)
        self.assertAlmostEqual(out["realized_r_total"], 60 * 0.5 - 40)
        self.assertAlmostEqual(out["expectancy_r"], (60 * 0.5 - 40) / 100)
        self.assertEqual(row["symbols"], ["BTCUSD", "EURUSD", "USDJPY", "XAUUSD"])   # 025 seeded membership
        self.assertEqual(row["last_event_at"], T0 + timedelta(hours=129, minutes=5))
        self.assertEqual(row["stats_scope"], "FULL_CANONICAL_HISTORY")

    def test_page_model_carries_everything_needed_to_render(self):
        page = self.repo.strategy_page(STRATEGY)
        for key in ("lifecycle", "stats", "performance", "observations", "events", "configuration",
                    "technical_metadata", "trade_management", "instruments"):
            self.assertIn(key, page)
        lifecycle = {s["key"]: s["count"] for s in page["lifecycle"]}
        self.assertEqual(lifecycle, {"SIGNALS": 130, "OPEN": 10, "TARGET_HIT": 60, "STOPPED": 40, "UNTRACKED": 20})
        perf = page["performance"]
        self.assertEqual((perf["closed"], perf["open"]), (100, 10))
        self.assertAlmostEqual(perf["series"][-1]["cumulative_realized_r"], -10.0)
        self.assertEqual({b["instrument"] for b in perf["by_instrument"]}, {"EURUSD", "XAUUSD"})
        self.assertEqual(len(page["observations"]), 50)                           # bounded list, stats unaffected
        self.assertTrue(all(o["status"] == "OPEN" for o in page["observations"][:10]))  # open first
        self.assertEqual(page["technical_metadata"]["engine_version"], "0a990dd5")
        self.assertEqual({i["canonical_instrument"]: i["provider_symbol"] for i in page["instruments"]}["XAUUSD"], "XAUUSDm")

    def test_effective_trade_management_is_reported_without_explicit_bindings(self):
        tm = self.repo.strategy_page(STRATEGY)["trade_management"]
        self.assertEqual(tm["resolution"], "DEFAULT_TM_NONE")
        self.assertEqual(tm["bindings"][0]["label"], "TM-NONE-1")
        self.assertEqual(tm["managed_trades"]["total"], 0)

    def test_unknown_strategy_is_none(self):
        self.assertIsNone(self.repo.strategy_page("NOPE"))

    def test_definition_seed_matches_the_production_strategy(self):
        [row] = [s for s in self.repo.list_strategies() if s["strategy_id"] == STRATEGY]
        self.assertEqual((row["strategy_version"], row["adapter"], row["enabled"], row["default_instance_id"]),
                         ("V1", "ContextStructureRetraceAdapter", True, "phase6"))
        self.assertEqual(row["routes"], {"audit": True, "shadow_execution": True, "distribution_queue": True})

    def test_strategy_instances_are_children_of_the_parent_definition(self):
        instances = self.repo.strategy_instances(STRATEGY)
        self.assertEqual([(row["instance_id"], row["strategy_id"], row["enabled"]) for row in instances],
                         [("phase6", STRATEGY, True)])


if __name__ == "__main__":
    unittest.main()
