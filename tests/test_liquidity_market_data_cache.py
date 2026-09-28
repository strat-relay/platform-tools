"""Liquidity live runtime on the market-data cache (MARKET_DATA_SOURCE=REDIS): the cached
LiveMarketSnapshot equals what the bridge adapter builds from the same broker, missing/stale data
fails closed, and one failed cycle no longer kills the runtime."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    import fakeredis
except ImportError:  # pragma: no cover - CI installs requirements-test.txt
    fakeredis = None

from test_market_data_cache import START, TF_SECONDS, FakeBridge  # noqa: E402


class BridgeClient:
    """ReadOnlyBridgeClient over the same fake broker (what the bridge adapter calls)."""

    def __init__(self, bridge):
        self.bridge = bridge

    def quote(self, symbol):
        return self.bridge("mt5_quote", {"symbol": symbol})

    def symbol_info(self, symbol):
        snap = self.bridge("mt5_symbol_snapshot", {"symbol": symbol, "timeframes": ["M5"], "limit": 2})
        return snap["symbol_info"]

    def rates(self, symbol, timeframe, limit=20):
        snap = self.bridge("mt5_symbol_snapshot", {"symbol": symbol, "timeframes": [timeframe], "limit": limit})
        return snap["rates"][timeframe]


@unittest.skipIf(fakeredis is None, "fakeredis not installed (requirements-test.txt)")
class CachedLiquiditySnapshotTests(unittest.TestCase):
    def setUp(self):
        from market_data_cache.collector import MarketDataCollector
        from market_data_cache.store import MarketDataStore
        self.now = {"t": float(START + 3)}
        self.bridge = FakeBridge(lambda: self.now["t"])
        self.store = MarketDataStore(fakeredis.FakeRedis())
        self.collector = MarketDataCollector(self.store, self.bridge, bar_symbols=lambda: ["BTCUSDm"],
                                             clock=lambda: self.now["t"])

    def cached(self, **kw):
        from liquidity_market_data import CachedLiquidityMarketData
        return CachedLiquidityMarketData(self.store, clock=lambda: self.now["t"], **kw)

    def test_cached_snapshot_equals_the_bridge_adapter_across_bar_closes(self):
        from liquidity_market_data import ReadOnlyLiquidityMarketData
        direct = ReadOnlyLiquidityMarketData(BridgeClient(self.bridge))
        for step in range(0, 3 * 3600, 60):
            self.now["t"] = START + 3 + step
            self.collector.tick()
            if self.store.snapshot("BTCUSDm")["fetched_at"] >= self.now["t"] - 1:
                calls = len(self.bridge.calls)
                a = self.cached().snapshot("BTCUSD", "BTCUSDm")
                self.assertEqual(len(self.bridge.calls), calls)            # no bridge call from the cache
                b = direct.snapshot("BTCUSD", "BTCUSDm")
                self.assertEqual((a.M5, a.M15), (b.M5, b.M15))
                self.assertEqual((len(a.M5), len(a.M15)), (159, 63))
                self.assertEqual(a.validated_by, "market-data-cache")

    def test_missing_stale_or_shallow_cache_fails_closed(self):
        from market_data_cache.reader import MarketDataUnavailable
        with self.assertRaises(MarketDataUnavailable):
            self.cached().snapshot("BTCUSD", "BTCUSDm")                    # nothing cached
        self.collector.tick()
        self.now["t"] += 331
        with self.assertRaises(MarketDataUnavailable):
            self.cached().snapshot("BTCUSD", "BTCUSDm")                    # stale
        self.now["t"] -= 331
        self.store.set_bars("BTCUSDm", "M5", self.store.bars("BTCUSDm", "M5")[-10:])
        with self.assertRaises(MarketDataUnavailable):
            self.cached().snapshot("BTCUSD", "BTCUSDm")                    # not deep enough

    def test_switch_defaults_to_the_bridge(self):
        import liquidity_market_data as lmd
        env = {k: v for k, v in os.environ.items() if k != "MARKET_DATA_SOURCE"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(lmd, "build_bridge_client", return_value="c"):
            self.assertIsInstance(lmd.build_liquidity_market_data("http://h:22347/mcp"), lmd.ReadOnlyLiquidityMarketData)
        with mock.patch.dict(os.environ, {"MARKET_DATA_SOURCE": "REDIS"}), \
                mock.patch("market_data_cache.reader.default_store", return_value=self.store):
            self.assertIsInstance(lmd.build_liquidity_market_data("http://h:22347/mcp"), lmd.CachedLiquidityMarketData)


class ServiceLoopTests(unittest.TestCase):
    def test_a_failed_cycle_is_logged_and_the_loop_continues(self):
        import liquidity_live_service as svc
        outcomes = [RuntimeError("bridge timeout"), RuntimeError("bridge timeout"), {"ok": True}, KeyboardInterrupt()]

        def run_once():
            result = outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        sleeps, audits = [], []
        with mock.patch.object(svc, "check_configuration"), mock.patch.object(svc, "run_once", side_effect=run_once), \
                mock.patch.object(svc, "configure_strategy_audit_logging"), \
                mock.patch.object(svc, "audit", side_effect=lambda e, **f: audits.append((e, f))), \
                mock.patch.object(svc.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(KeyboardInterrupt):
                svc.main()
        self.assertEqual([e for e, _ in audits], ["runner_started", "tick_failed", "tick_failed"])
        self.assertEqual(sleeps, [5.0, 10.0, 5.0])                          # backoff, then reset

    def test_configuration_errors_still_stop_the_process(self):
        import liquidity_live_service as svc
        env = {k: v for k, v in os.environ.items() if k != "LIQUIDITY_LIVE_RUNTIME_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(svc, "configure_strategy_audit_logging"), \
                mock.patch.object(svc, "audit"):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                svc.main()


if __name__ == "__main__":
    unittest.main()
