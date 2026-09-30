import os
import time
import unittest
from unittest import mock


class Store:
    def __init__(self, fetched_at=None):
        now = time.time() if fetched_at is None else fetched_at
        self._snapshot = {"fetched_at": now, "symbol_info": {"point": 0.01},
                          "quote": {"bid": 100.0, "ask": 100.2, "time": 1790770800}}
        self._bars = {
            tf: [{"time": 1790000000 + i * step, "open": 100 + i / 100,
                  "high": 101 + i / 100, "low": 99 + i / 100, "close": 100.5 + i / 100}
                 for i in range(400)]
            for tf, step in (("M5", 300), ("M15", 900), ("H1", 3600), ("H4", 14400))
        }
        self._state = {"healthy": True, "health_state": "HEALTHY", "forming": {"M5": self._bars["M5"][-1]["time"]}}

    def snapshot(self, _symbol):
        return self._snapshot

    def bars(self, _symbol, timeframe):
        return self._bars[timeframe]

    def state(self, _symbol):
        return self._state


class CanonicalMarketDataTests(unittest.TestCase):
    def test_context_and_liquidity_receive_identical_canonical_m5_candles(self):
        import context_structure_retrace_forward as context
        from liquidity_market_data import CachedLiquidityMarketData

        store = Store()
        with mock.patch.dict(os.environ, {"MARKET_DATA_SOURCE": "REDIS"}), \
             mock.patch("market_data_cache.reader.default_store", return_value=store):
            _, _, context_bars = context.read_symbol("XAUUSDm", "http://unused", limit=160)
        liquidity = CachedLiquidityMarketData(store, clock=time.time).snapshot("XAUUSD", "XAUUSDm")
        self.assertEqual(context_bars["M5"], list(liquidity.M5))
        self.assertEqual(context_bars["M5"][-1]["time"], liquidity.M5[-1]["time"])
        self.assertEqual(liquidity.data_health["source"], "REDIS_CANONICAL_CACHE")

    def test_stale_canonical_data_fails_closed_without_mcp_fallback(self):
        import context_structure_retrace_forward as context
        from market_data_cache.reader import MarketDataUnavailable

        store = Store(fetched_at=time.time() - 331)
        with mock.patch.dict(os.environ, {"MARKET_DATA_SOURCE": "REDIS"}), \
             mock.patch("market_data_cache.reader.default_store", return_value=store), \
             mock.patch.object(context, "bridge_read", side_effect=AssertionError("MCP fallback")):
            with self.assertRaises(MarketDataUnavailable):
                context.read_symbol("XAUUSDm", "http://unused", limit=160)

    def test_canonical_reader_rejects_gap_state_before_strategy_consumption(self):
        from market_data_cache.canonical import CanonicalMarketDataReader
        from market_data_cache.reader import MarketDataUnavailable

        store = Store()
        store._state.update(healthy=False, health_state="BACKFILLING", backfill_required=True)
        with self.assertRaisesRegex(MarketDataUnavailable, "gap state"):
            CanonicalMarketDataReader(store).get_bars("XAUUSD", "XAUUSDm", "M5", 320)


if __name__ == "__main__":
    unittest.main()
