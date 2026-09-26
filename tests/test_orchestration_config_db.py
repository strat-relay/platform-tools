"""The signal orchestrator's configuration lives in PostgreSQL (migration 029); platform.json is gone.

Proves: import -> load_config() round-trips the exact dict the orchestrator used to read from the
file (settings, accounts, portfolios, strategies incl. trade_management and extra keys), the
orchestrator fails closed when a database is configured but nothing was imported, the import is
dry-run by default and idempotent, and the API's bridge URL comes from runtime_setting.
"""
from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from orchestration import config as orch_config  # noqa: E402
from orchestration.registry import PortfolioRegistry, StrategyRegistry  # noqa: E402
from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import apply_migrations, connect  # noqa: E402
from scripts.import_platform_config import asset_class, import_config  # noqa: E402

# The shape of the production ConfigMap (values illustrative), plus a trade_management block and an
# extra per-strategy key to prove the import is lossless.
PRODUCTION_LIKE = {
    "schema_version": "orchestration-platform-v1", "execution_mode": "SHADOW",
    "mcp_url": "http://10.0.0.1:22347/mcp", "execution_mcp_url": None, "execution_transport_verified": False,
    "sizing_scenarios": [0.0025, 0.005, 0.01, 0.02],
    "symbol_mappings": {"XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSDm", "USDJPY": "USDJPYm", "EURUSD": "EURUSDm"},
    "accounts": [{"account_id": "exness-shadow-k8s", "broker": "Exness", "broker_environment": "SHADOW",
                  "broker_account_reference": "REDACTED", "currency": "USD", "enabled": True, "execution_mode": "SHADOW"}],
    "portfolios": [{"portfolio_id": "portfolio-shadow-k8s", "name": "Kubernetes Shadow Portfolio", "enabled": True,
                    "base_currency": "USD", "sizing_policy_id": "equity-fractional-v1",
                    "account_ids": ["exness-shadow-k8s"], "strategy_ids": ["CONTEXT_STRUCTURE_RETRACE_V1"]}],
    "strategies": [{"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1", "enabled": True,
                    "adapter": "ContextStructureRetraceAdapter", "portfolio_routing": True,
                    "routes": {"audit": True, "shadow_execution": True, "distribution_queue": True},
                    "trade_management": {"policy": "tm-breakeven-trail.v1", "label": "TM-BREAKEVEN-TRAIL-1-CONTEXT-DEFAULT",
                                         "params": {"breakeven_trigger_r": 1.0, "trail_trigger_r": 1.5, "trail_distance_r": 0.5}}}],
}


class WithoutDatabaseTests(unittest.TestCase):
    def test_no_database_configured_uses_defaults_and_never_a_file(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TRADING_POSTGRES_DSN", "PGHOST")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(orch_config.load_config(), orch_config.DEFAULT_CONFIG)
        self.assertFalse((ROOT / "orchestration/config/platform.json").exists())
        self.assertFalse(hasattr(orch_config, "CONFIG_PATH"))

    def test_asset_class_inference_for_imported_mappings(self):
        self.assertEqual([asset_class(x) for x in ("XAUUSD", "BTCUSD", "ETHUSD", "EURUSD", "US500")],
                         ["METAL", "CRYPTO", "CRYPTO", "FX", "OTHER"])

    def test_dry_run_plans_without_connecting(self):
        plan = import_config(PRODUCTION_LIKE, apply=False,
                             connect_fn=lambda: (_ for _ in ()).throw(AssertionError("no DB in dry run")))
        self.assertTrue(plan["dry_run"])
        self.assertIn("mcp_url", plan["settings"])
        self.assertEqual(plan["strategies"], ["CONTEXT_STRUCTURE_RETRACE_V1"])


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class RealPostgresConfigTests(unittest.TestCase):
    def setUp(self):
        from test_integration_tm_membership_runner import FreshDatabase
        self.db = FreshDatabase()
        with self.db.connect() as conn:
            apply_migrations(conn)
        self.env = patch.dict(os.environ, {"TRADING_POSTGRES_DSN": self.db.dsn})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.db.drop()

    def test_orchestrator_fails_closed_until_configuration_is_imported(self):
        with self.assertRaises(orch_config.PlatformConfigUnavailable):
            orch_config.load_config()

    def test_import_round_trips_the_exact_orchestrator_config(self):
        result = import_config(copy.deepcopy(PRODUCTION_LIKE), apply=True, connect_fn=self.db.connect)
        self.assertFalse(result["dry_run"])
        loaded = orch_config.load_config()
        expected = {k: v for k, v in PRODUCTION_LIKE.items() if k != "symbol_mappings"}
        self.assertEqual(loaded, expected)
        # Orchestrator registries work unchanged on the database-backed config.
        self.assertEqual([s["strategy_id"] for s in StrategyRegistry(loaded).enabled()], ["CONTEXT_STRUCTURE_RETRACE_V1"])
        self.assertEqual(len(PortfolioRegistry(loaded).routes_for("CONTEXT_STRUCTURE_RETRACE_V1")), 1)

    def test_import_is_idempotent_and_updates_only_what_changed(self):
        import_config(copy.deepcopy(PRODUCTION_LIKE), apply=True, connect_fn=self.db.connect)
        again = import_config(copy.deepcopy(PRODUCTION_LIKE), apply=True, connect_fn=self.db.connect)
        self.assertEqual(again["changed"], {k: [] for k in again["changed"]})
        changed = copy.deepcopy(PRODUCTION_LIKE)
        changed["execution_mode"] = "SHADOW_V2"
        changed["symbol_mappings"]["ETHUSD"] = "ETHUSDm"
        third = import_config(changed, apply=True, connect_fn=self.db.connect)
        self.assertEqual((third["changed"]["settings"], third["changed"]["symbol_mappings"]), (["execution_mode"], ["ETHUSD"]))
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT revision FROM platform.runtime_setting WHERE key = 'execution_mode'")
            self.assertEqual(cur.fetchone()[0], 2)
            cur.execute("""SELECT provider_symbol, asset_class FROM platform.instrument_provider_mapping
                           WHERE canonical_instrument = 'ETHUSD'""")
            self.assertEqual(cur.fetchone(), ("ETHUSDm", "CRYPTO"))

    def test_strategy_display_fields_survive_import(self):
        import_config(copy.deepcopy(PRODUCTION_LIKE), apply=True, connect_fn=self.db.connect)
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT display_name, default_instance_id, trade_management->>'policy'
                           FROM platform.strategy_definition WHERE strategy_id = 'CONTEXT_STRUCTURE_RETRACE_V1'""")
            self.assertEqual(cur.fetchone(), ("Context Structure Retrace", "phase6", "tm-breakeven-trail.v1"))

    def test_api_bridge_url_comes_from_runtime_setting(self):
        from platform_api.control import PlatformControlApi, PlatformControlRepository
        import_config(copy.deepcopy(PRODUCTION_LIKE), apply=True, connect_fn=self.db.connect)
        env = {k: v for k, v in os.environ.items() if k != "MT5_BRIDGE_MCP_URL"}
        with patch.dict(os.environ, env, clear=True):
            api = PlatformControlApi(repository=PlatformControlRepository(), environ={}, v2_risk_api=object(),
                                     trade_manager_mode_api=object())
            self.assertEqual(api.bridge_reader.endpoint, "http://10.0.0.1:22347/mcp")
        api = PlatformControlApi(repository=PlatformControlRepository(), environ={"MT5_BRIDGE_MCP_URL": "http://x/mcp"},
                                 v2_risk_api=object(), trade_manager_mode_api=object())
        self.assertEqual(api.bridge_reader.endpoint, "http://x/mcp")   # explicit env still wins


if __name__ == "__main__":
    unittest.main()
