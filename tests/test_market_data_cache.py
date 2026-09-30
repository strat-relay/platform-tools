"""Market-data cache (market_data_cache/): completed bars, metadata, quotes.

Central property: the Context runner's cached read_symbol() returns exactly what a fresh full
`mt5_symbol_snapshot` returns (after dropping the forming bar), across hours of simulated time,
collector outages, and broker bar revisions - while issuing a small fraction of bridge commands.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

try:
    import fakeredis
except ImportError:  # pragma: no cover - CI installs requirements-test.txt
    fakeredis = None

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
START = 1_790_400_000 - (1_790_400_000 % 14400)        # an H4 boundary, UTC == broker server time


def _bar(symbol: str, tf: str, t: int, revision: int = 0) -> dict:
    seed = (sum(map(ord, symbol)) * 7 + TF_SECONDS[tf] + t // 60 + revision * 13) % 1000
    o = 1.1 + seed / 100000
    return {"time": t, "open": round(o, 5), "high": round(o + 0.0004, 5), "low": round(o - 0.0003, 5),
            "close": round(o + 0.0001, 5), "tick_volume": seed, "spread": 12, "real_volume": 0}


class FakeBridge:
    """Deterministic MT5: bars exist for every tf slot; the last returned row is the forming bar."""

    def __init__(self, clock):
        self.clock, self.calls, self.revised, self.down, self.frozen_forming = clock, [], {}, False, None

    def __call__(self, tool, args):
        self.calls.append((tool, args.get("symbol"), args.get("limit")))
        if self.down:
            raise ConnectionError("read bridge unreachable")
        symbol = args.get("symbol")
        if tool == "mt5_terminal_info":
            return {"connected": True}
        if tool == "mt5_symbol_info":
            return {"symbol": symbol, "tick_size": 0.00001, "digits": 5}
        if tool == "mt5_quote":
            return {"symbol": symbol, "bid": 1.1, "ask": 1.1002, "time": int(self.clock())}
        assert tool == "mt5_symbol_snapshot", tool
        now = int(self.frozen_forming or self.clock())
        rates = {}
        for tf in args["timeframes"]:
            step = TF_SECONDS[tf]
            forming = now - now % step
            times = [forming - step * i for i in range(args["limit"])][::-1]
            rates[tf] = {"rates": [_bar(symbol, tf, t, self.revised.get((symbol, tf, t), 0)) for t in times]}
        return {"healthy": True, "source_read_health": True, "symbol_info": {"symbol": symbol, "point": 0.00001},
                "quote": {"bid": 1.1, "ask": 1.1002, "time": int(self.clock())}, "rates": rates}

    def direct_read_symbol(self, symbol, limit=320):
        """What the runner's bridge read_symbol returns today (same _completed() rule)."""
        snap = self(("mt5_symbol_snapshot"), {"symbol": symbol, "timeframes": list(TF_SECONDS), "limit": limit})
        return snap["symbol_info"], {tf: r["rates"][:-1] for tf, r in snap["rates"].items()}


@unittest.skipIf(fakeredis is None, "fakeredis not installed (requirements-test.txt)")
class MarketDataCacheTest(unittest.TestCase):
    SYMBOLS = ["EURUSDm", "XAUUSDm", "BTCUSDm"]

    def setUp(self):
        from market_data_cache.collector import MarketDataCollector
        from market_data_cache.store import MarketDataStore
        self.now = {"t": float(START + 3)}
        self.bridge = FakeBridge(lambda: self.now["t"])
        self.store = MarketDataStore(fakeredis.FakeRedis())
        self.collector = MarketDataCollector(self.store, self.bridge, bar_symbols=lambda: list(self.SYMBOLS),
                                             clock=lambda: self.now["t"])

    def read(self, symbol, **kw):
        from market_data_cache.reader import read_symbol_cached
        return read_symbol_cached(self.store, symbol, 320, now=self.now["t"], **kw)

    def assert_parity(self, symbol):
        contract, quote, bars = self.read(symbol)
        calls = len(self.bridge.calls)
        direct_contract, direct_bars = self.bridge.direct_read_symbol(symbol)
        del self.bridge.calls[calls:]                       # the reference read is not collector traffic
        self.assertEqual(contract, direct_contract)
        for tf in TF_SECONDS:
            self.assertEqual(bars[tf], direct_bars[tf], f"{symbol} {tf} at {self.now['t']}")


class CompletedBarTests(MarketDataCacheTest):
    def test_cached_read_equals_a_fresh_full_snapshot_for_hours(self):
        for step in range(0, 6 * 3600, 15):                 # a 15 s runner poll for six hours
            self.now["t"] = START + 3 + step
            self.collector.tick()
            if self.store.snapshot("EURUSDm")["fetched_at"] >= self.now["t"] - 1:   # just refreshed
                for symbol in self.SYMBOLS:
                    self.assert_parity(symbol)

    def test_bridge_commands_drop_to_one_per_symbol_per_bar_close(self):
        polls = 0
        for step in range(0, 3600, 15):
            self.now["t"] = START + 3 + step
            self.collector.tick()
            polls += len(self.SYMBOLS)                       # what the runner's direct reads would cost
        snapshots = [c for c in self.bridge.calls if c[0] == "mt5_symbol_snapshot"]
        self.assertEqual(len(snapshots), len(self.SYMBOLS) * 12)          # 1 full + 11 incremental per symbol
        self.assertEqual(sum(1 for c in snapshots if c[2] == 321), len(self.SYMBOLS))
        self.assertLessEqual(len(snapshots) * 20, polls)                   # 20x fewer commands

    def test_an_outage_longer_than_the_incremental_window_refills_with_a_full_fetch(self):
        self.collector.tick()
        self.bridge.down = True
        for step in range(15, 3 * 3600, 60):
            self.now["t"] = START + 3 + step
            self.collector.tick()
        self.assertFalse(self.store.state("EURUSDm")["healthy"])
        self.bridge.down = False
        self.now["t"] = START + 3 * 3600 + 30   # past the bounded probe backoff
        result = self.collector.tick()
        self.assertEqual(len(result["bars"]), 1)   # gradual recovery releases one snapshot first
        self.assertTrue(result["bars"][0]["full"] and "does not reach" in result["bars"][0]["reason"])
        for _ in range(len(self.SYMBOLS) + 1):
            self.now["t"] += 1
            self.collector.tick()
        for symbol in self.SYMBOLS:
            self.assert_parity(symbol)

    def test_a_revised_broker_bar_forces_a_full_refetch(self):
        self.collector.tick()
        self.now["t"] = START + 303
        revised_time = START - 600
        self.bridge.revised[("EURUSDm", "M5", revised_time)] = 1
        result = {r["symbol"]: r for r in self.collector.tick()["bars"]}
        self.assertTrue(result["EURUSDm"]["full"])
        self.assertIn("differs", result["EURUSDm"]["reason"])
        self.assertFalse(result["XAUUSDm"]["full"])
        self.assert_parity("EURUSDm")

    def test_no_new_bar_retries_a_few_times_then_waits_for_the_next_close(self):
        self.collector.tick()
        self.bridge.frozen_forming = START + 3                # market closed: forming bar never advances
        fetches = []
        for step in range(300, 598, 1):                        # up to just before the next close (+600 s)
            self.now["t"] = START + 3 + step
            fetches += [r for r in self.collector.tick()["bars"] if r["symbol"] == "EURUSDm"]
        self.assertEqual(len(fetches), 1 + 3)                 # boundary fetch + 3 retries, then quiet
        self.now["t"] = START + 603
        self.assertEqual([r["symbol"] for r in self.collector.tick()["bars"]], self.SYMBOLS)   # next close

    def test_failure_keeps_cached_bars_and_the_reader_serves_them_while_fresh(self):
        self.collector.tick()
        before = self.store.bars("EURUSDm", "M5")
        self.now["t"] = START + 303
        self.bridge.down = True
        self.collector.tick()
        self.assertEqual(self.store.bars("EURUSDm", "M5"), before)
        self.assertEqual(self.store.health()["status"], "degraded")
        self.read("EURUSDm")                                  # 300 s old: still within 330 s

    def test_stale_missing_or_shallow_cache_fails_closed(self):
        from market_data_cache.reader import MarketDataUnavailable
        with self.assertRaises(MarketDataUnavailable):
            self.read("EURUSDm")                              # nothing cached yet
        self.collector.tick()
        self.now["t"] += 331
        with self.assertRaises(MarketDataUnavailable):
            self.read("EURUSDm")
        self.now["t"] -= 331
        from market_data_cache.reader import read_symbol_cached
        with self.assertRaises(MarketDataUnavailable):
            read_symbol_cached(self.store, "EURUSDm", 500, now=self.now["t"])   # deeper than cached

    def test_provenance_identifies_the_cache(self):
        self.collector.tick()
        *_, producer = self.read("EURUSDm", include_provenance=True)
        self.assertEqual((producer["provenance_source"], producer["source_read_health"]), ("MARKET_DATA_CACHE", True))


class MetadataAndQuoteTests(MarketDataCacheTest):
    def test_snapshot_metadata_is_stored_and_extra_symbols_refresh_on_their_interval(self):
        from market_data_cache.reader import read_metadata
        self.collector.metadata_symbols = lambda: ["ETHUSDm"]
        self.collector.tick()
        self.assertEqual(read_metadata(self.store, "EURUSDm", now=self.now["t"])["symbol"], "EURUSDm")
        self.assertEqual(read_metadata(self.store, "ETHUSDm", now=self.now["t"])["symbol"], "ETHUSDm")
        self.now["t"] += 60
        self.collector.tick()
        self.assertEqual(sum(1 for c in self.bridge.calls if c[0] == "mt5_symbol_info"), 1)
        self.now["t"] += 300
        self.collector.tick()
        self.assertEqual(sum(1 for c in self.bridge.calls if c[0] == "mt5_symbol_info"), 2)

    def test_quotes_are_capped_per_pass_and_rotate(self):
        symbols = [f"S{i}m" for i in range(25)]
        self.collector.quote_symbols = lambda: symbols
        self.collector.bar_symbols = lambda: []
        seen = []
        for _ in range(3):
            seen.append(set(self.collector.tick()["quotes"]))
            self.now["t"] += 1
        self.assertEqual([len(s) for s in seen], [10, 10, 10])          # never more than the cap per pass
        self.assertEqual(set().union(*seen), set(symbols))               # all 25 served within 3 passes
        self.assertTrue(set(symbols) - seen[0] - seen[1] <= seen[2])      # never-refreshed ones go first

    def test_cached_quote_client_serves_fresh_quotes_and_fails_closed_otherwise(self):
        from market_data_cache.reader import CachedQuoteClient, MarketDataUnavailable
        passthrough = mock.Mock()
        client = CachedQuoteClient(self.store, passthrough, max_age=15, clock=lambda: self.now["t"])
        with self.assertRaises(MarketDataUnavailable):
            client.quote("EURUSDm")
        self.collector.quote_symbols = lambda: ["EURUSDm"]
        self.collector.tick()
        self.assertEqual(client.quote("EURUSDm")["bid"], 1.1)
        self.now["t"] += 16
        with self.assertRaises(MarketDataUnavailable):
            client.quote("EURUSDm")
        client.rates("EURUSDm", "M5", limit=1)
        passthrough.rates.assert_called_once_with("EURUSDm", "M5", limit=1)


class IntegrationTests(unittest.TestCase):
    def test_runner_reads_the_cache_without_touching_the_bridge_and_keeps_its_fingerprint(self):
        import context_structure_retrace_forward as runner
        cache = mock.Mock(return_value=({"c": 1}, {"q": 1}, {"M5": []}))
        with mock.patch.dict(os.environ, {"MARKET_DATA_SOURCE": "REDIS"}), \
                mock.patch("market_data_cache.reader.read_symbol_cached", cache), \
                mock.patch("market_data_cache.reader.default_store", return_value="store"), \
                mock.patch.object(runner, "bridge_read", side_effect=AssertionError("bridge read")):
            self.assertEqual(runner.read_symbol("EURUSDm", "http://x/mcp"), ({"c": 1}, {"q": 1}, {"M5": []}))
        cache.assert_called_once_with("store", "EURUSDm", 320, False)
        self.assertEqual(runner.decision_code_hash(), runner.FROZEN_DECISION_CODE_HASH)

    def test_context_requires_canonical_redis_source_and_never_falls_back_to_bridge(self):
        import context_structure_retrace_forward as runner
        env = {k: v for k, v in os.environ.items() if k not in ("MARKET_DATA_SOURCE", "TM_MARKET_DATA_SOURCE")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(runner, "bridge_read", side_effect=AssertionError("bridge fallback")) as bridge:
            with self.assertRaisesRegex(RuntimeError, "requires MARKET_DATA_SOURCE=REDIS"):
                runner.read_symbol("EURUSDm", "http://x/mcp")
        bridge.assert_not_called()

    def test_trade_manager_switch_wraps_quotes_only(self):
        from market_data_cache.reader import CachedQuoteClient
        from trade_management.runtime import market_data_live
        with mock.patch.dict(os.environ, {"TM_MARKET_DATA_SOURCE": "REDIS"}), \
                mock.patch("market_data_cache.reader.default_store", return_value=mock.Mock()):
            client = market_data_live.build_bridge_client("http://host:22347/mcp")
        self.assertIsInstance(client, CachedQuoteClient)
        with self.assertRaises(ValueError):
            market_data_live.build_bridge_client("http://host:22348/mcp")


if __name__ == "__main__":
    unittest.main()
