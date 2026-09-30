"""Regression coverage for timestamp-range market-data recovery."""
from __future__ import annotations

import json
import unittest

from market_data_cache.collector import MarketDataCollector
from market_data_cache.store import MarketDataStore, TIMEFRAMES, TIMEFRAME_SECONDS, internal_missing_times


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class RangeBridge:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    @staticmethod
    def bar(symbol, timeframe, timestamp):
        seed = (timestamp // 60 + len(symbol) + len(timeframe)) % 1000
        price = 100.0 + seed / 10000
        return {"time": timestamp, "open": price, "high": price + .1, "low": price - .1,
                "close": price + .02, "tick_volume": seed, "spread": 2, "real_volume": 0}

    def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "mt5_rates_range":
            step = TIMEFRAME_SECONDS[args["timeframe"]]
            return {"rates": [self.bar(args["symbol"], args["timeframe"], timestamp)
                              for timestamp in range(args["start_timestamp"], args["end_timestamp"], step)]}
        if tool == "mt5_symbol_snapshot":
            rates = {}
            now = int(self.clock())
            for timeframe in TIMEFRAMES:
                step = TIMEFRAME_SECONDS[timeframe]
                forming = now - now % step
                rates[timeframe] = {"rates": [self.bar(args["symbol"], timeframe, timestamp)
                                               for timestamp in range(forming - step * (args["limit"] - 1), forming + step, step)]}
            return {"healthy": True, "source_read_health": True,
                    "symbol_info": {"symbol": args["symbol"], "point": .01},
                    "quote": {"bid": 100, "ask": 100.1, "time": now}, "rates": rates}
        raise AssertionError(tool)


class MarketDataRecoveryTest(unittest.TestCase):
    def test_large_timestamp_gap_is_backfilled_in_idempotent_pages(self):
        clock = {"now": 1_800_000_003}
        bridge = RangeBridge(lambda: clock["now"])
        store = MarketDataStore(MemoryRedis())
        collector = MarketDataCollector(store, bridge, bar_symbols=lambda: ["XAUUSDm"],
                                        clock=lambda: clock["now"])

        collector.tick()
        clock["now"] += 2 * 86400
        result = collector.tick()

        self.assertTrue(result["bars"][0]["ok"])
        self.assertEqual(store.state("XAUUSDm")["health_state"], "HEALTHY")
        self.assertTrue(any(tool == "mt5_rates_range" for tool, _ in bridge.calls))
        self.assertTrue(all(args["page_size"] <= 500 for tool, args in bridge.calls if tool == "mt5_rates_range"))
        for timeframe in TIMEFRAMES:
            self.assertEqual(internal_missing_times(store.bars("XAUUSDm", timeframe), timeframe), [])

        # A third pass is a normal incremental pass and must not duplicate recovered bars.
        before = {tf: len(store.bars("XAUUSDm", tf)) for tf in TIMEFRAMES}
        clock["now"] += 300
        collector.tick()
        for timeframe in TIMEFRAMES:
            rows = store.bars("XAUUSDm", timeframe)
            self.assertLessEqual(len(rows), before[timeframe] + 1)
            self.assertEqual(len({int(row["time"]) for row in rows}), len(rows))


if __name__ == "__main__":
    unittest.main()
