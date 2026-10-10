"""Instrument provider mapping lives in the database (migration 027), not platform.json.

Covers: the catalog API and membership validation read platform.instrument_provider_mapping, the
Context runner and the TM market-data provider resolve provider symbols from it, and the operator
seed script (dry-run by default, idempotent, never re-enables a DISABLED membership).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from platform_api.instrument_membership import CatalogInstrument  # noqa: E402
from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import apply_migrations, connect  # noqa: E402
from scripts.seed_instrument_mappings import load_mappings, seed  # noqa: E402

STRATEGY = "CONTEXT_STRUCTURE_RETRACE_V1"
PROBE = {"probed": {"FX": 3, "CRYPTO": 2}, "found": {
    "FX": {"GBPUSD": {"provider_symbol": "GBPUSDm", "trade_mode": 4, "trade_enabled": True},
           "USDTRY": {"provider_symbol": "USDTRYm", "trade_mode": 0, "trade_enabled": False}},
    "CRYPTO": {"ETHUSD": {"provider_symbol": "ETHUSDm", "trade_mode": 4, "trade_enabled": True}}}}


class ProbeInputTests(unittest.TestCase):
    def test_only_tradable_probe_entries_are_mapped(self):
        rows = load_mappings(PROBE)
        self.assertEqual({(r["canonical"], r["provider_symbol"], r["asset_class"]) for r in rows},
                         {("GBPUSD", "GBPUSDm", "FX"), ("ETHUSD", "ETHUSDm", "CRYPTO")})

    def test_dry_run_writes_nothing(self):
        class NoWrites:
            def __getattr__(self, name):
                raise AssertionError(f"dry run called {name}")
        summary = seed(NoWrites(), load_mappings(PROBE), apply=False, enable=(STRATEGY, "phase6"))
        self.assertEqual((summary["dry_run"], sorted(summary["mapped"])), (True, ["ETHUSD", "GBPUSD"]))


class ApiWiringTests(unittest.TestCase):
    def api(self, catalog):
        from platform_api.control import PlatformControlApi

        class Membership:
            saved = []

            def list_catalog(self):
                return catalog

            def save_membership(self, *args):
                self.saved.append(args)
                return {"canonical_instrument": args[2], "state": args[3]}

        class Repo:
            def execution_runtime_status(self):
                return {}

        class FakeMgmtApi:
            def handle(self, method, path, body):
                return None

            def registry_rows(self):
                return []

            def sync_instance_instrument(self, *_args):
                return False

        api = PlatformControlApi(repository=Repo(), environ={}, bridge_reader=object(), v2_risk_api=object(),
                                 trade_manager_mode_api=object(), strategy_config_path="/nonexistent/platform.json",
                                 strategy_mgmt_api=FakeMgmtApi())
        class Catalog:  # membership guards (tests/test_strategy_instances.py) need PostgreSQL
            def check_membership_change(self, *args):
                return None

        api.instrument_membership = Membership()
        api.strategy_catalog = Catalog()
        return api

    def test_catalog_comes_from_the_repository_not_platform_json(self):
        api = self.api([CatalogInstrument("ETHUSD", "MT5", "ETHUSDm", "Ethereum", "CRYPTO")])
        status, body = api.execute("GET", "/api/v1/instruments")
        self.assertEqual((status, body["source"]), (200, "canonical_postgres"))
        self.assertEqual(body["data"][0]["asset_class"], "CRYPTO")

    def test_membership_requires_an_active_mapping(self):
        api = self.api([CatalogInstrument("ETHUSD", "MT5", "ETHUSDm", "Ethereum", "CRYPTO")])
        ok, _ = api.execute("POST", f"/api/v1/strategies/{STRATEGY}/instruments",
                            json.dumps({"canonicalInstrument": "ethusd", "state": "ACTIVE"}).encode())
        rejected, body = api.execute("POST", f"/api/v1/strategies/{STRATEGY}/instruments",
                                     json.dumps({"canonicalInstrument": "ADAUSD", "state": "ACTIVE"}).encode())
        self.assertEqual((ok, rejected, body["error"]), (200, 409, "UNSUPPORTED_INSTRUMENT"))

    def test_no_live_code_reads_symbol_mappings_from_config(self):
        for path in ("platform_api/instrument_membership.py", "platform_api/control.py",
                     "context_structure_retrace_forward.py", "trade_management/runtime/market_data_live.py",
                     "orchestration/config.py"):
            self.assertNotIn("symbol_mappings", (ROOT / path).read_text(), path)
        self.assertFalse((ROOT / "orchestration/config/platform.json").exists())   # the file is gone


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class RealPostgresMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_integration_tm_membership_runner import FreshDatabase
        cls.db = FreshDatabase()
        with cls.db.connect() as conn:
            apply_migrations(conn)
        from platform_api.instrument_membership import InstrumentMembershipRepository
        cls.repo = InstrumentMembershipRepository(connect_fn=lambda readonly=False: cls.db.connect(readonly=readonly))

    @classmethod
    def tearDownClass(cls):
        cls.db.drop()

    def test_migration_seeds_the_former_platform_json_mappings(self):
        catalog = {c.canonical_instrument: c for c in self.repo.list_catalog()}
        self.assertLessEqual({"XAUUSD", "BTCUSD", "USDJPY", "EURUSD"}, set(catalog))
        self.assertEqual((catalog["XAUUSD"].provider_symbol, catalog["XAUUSD"].asset_class), ("XAUUSDm", "METAL"))

    def test_upsert_is_idempotent_and_disabled_mappings_leave_catalog_and_runner(self):
        import context_structure_retrace_forward as fwd
        name = f"X{uuid.uuid4().hex[:5].upper()}"
        first = self.repo.save_mapping(name, f"{name}m", "FX", actor="t")
        again = self.repo.save_mapping(name, f"{name}m", "FX", actor="t")
        self.assertEqual((first["revision"], first["changed"], again["revision"], again["changed"]), (1, True, 1, False))
        self.repo.save_membership(STRATEGY, "phase6", name, "ACTIVE", None, "t")
        with patch.dict(os.environ, {"TRADING_POSTGRES_DSN": self.db.dsn}):
            self.assertIn(f"{name}m", fwd.load_active_membership(Namespace(symbols=[]))[0])
            disabled = self.repo.save_mapping(name, f"{name}m", "FX", state="DISABLED", actor="t")
            self.assertEqual(disabled["revision"], 2)
            self.assertNotIn(f"{name}m", fwd.load_active_membership(Namespace(symbols=[]))[0])
        self.assertNotIn(name, [c.canonical_instrument for c in self.repo.list_catalog()])
        with self.assertRaises(ValueError):
            self.repo.save_mapping(name, f"{name}m", "NOT_A_CLASS", actor="t")

    def test_seed_script_maps_enables_and_respects_disabled_membership(self):
        self.repo.save_mapping("GBPUSD", "GBPUSDm", "FX", actor="t")
        self.repo.save_membership(STRATEGY, "phase6", "GBPUSD", "DISABLED", None, "operator")
        summary = seed(self.repo, load_mappings(PROBE), apply=True, enable=(STRATEGY, "phase6"))
        self.assertIn("ETHUSD", summary["enabled"])
        self.assertIn("GBPUSD=DISABLED", summary["membership_kept"])
        members = {r["canonical_instrument"]: r["state"] for r in self.repo.list_membership(STRATEGY, "phase6")}
        self.assertEqual((members["ETHUSD"], members["GBPUSD"]), ("ACTIVE", "DISABLED"))
        self.assertNotIn("USDTRY", {c.canonical_instrument for c in self.repo.list_catalog()})   # not tradable
        rerun = seed(self.repo, load_mappings(PROBE), apply=True, enable=(STRATEGY, "phase6"))
        self.assertEqual(rerun["enabled"], [])

    def test_tm_market_data_resolves_from_the_database(self):
        import trade_management.runtime.market_data_live as live
        self.repo.save_mapping("ETHUSD", "ETHUSDm", "CRYPTO", actor="t")
        env = {k: v for k, v in os.environ.items() if k != "P4_BROKER_SYMBOL_MAP_JSON"}
        env["TRADING_POSTGRES_DSN"] = self.db.dsn
        with patch.dict(os.environ, env, clear=True):
            live._mapping_cache = None
            self.assertEqual(live.resolve_broker_symbol("ETHUSD"), "ETHUSDm")
            with self.assertRaises(live.BrokerSymbolUnavailable):
                live.resolve_broker_symbol("NOPE")
        live._mapping_cache = None


if __name__ == "__main__":
    unittest.main()
