"""Collector liveness vs data health (market_data_cache/health.py).

Incident 2026-09-28: md:health was stamped with the pass *start* and written only at pass end, so a
50-minute catch-up pass behind a degraded bridge looked like a 50-minute stall. These tests run a
62-symbol pass where every bridge call takes 40 s and prove the heartbeat keeps advancing, the pass
start stays fixed, progress advances per symbol, the completed-pass stamp moves only at the end,
and the probe reports PROCESS=HEALTHY / DATA=DEGRADED while catching up.
"""
from __future__ import annotations

import unittest

try:
    import fakeredis
except ImportError:  # pragma: no cover - CI installs requirements-test.txt
    fakeredis = None

from test_market_data_cache import START, FakeBridge

CALL_SECONDS = 40.0
SYMBOLS = [f"SYM{i:02d}m" for i in range(62)]


@unittest.skipIf(fakeredis is None, "fakeredis not installed (requirements-test.txt)")
class CollectorHealthTests(unittest.TestCase):
    def setUp(self):
        from market_data_cache.collector import MarketDataCollector
        from market_data_cache.store import MarketDataStore
        self.now = {"t": float(START + 3)}
        fake = FakeBridge(lambda: self.now["t"])
        self.failing = {"SYM05m"}

        def slow_bridge(tool, args):                       # every bridge call takes CALL_SECONDS
            self.now["t"] += CALL_SECONDS
            if args.get("symbol") in self.failing:
                raise TimeoutError("MT5_SYMBOL_SNAPSHOT_TIMEOUT")
            return fake(tool, args)

        self.store = MarketDataStore(fakeredis.FakeRedis())
        self.writes: list[dict] = []
        original = self.store.set_health
        self.store.set_health = lambda value: (self.writes.append(dict(value)), original(value))[1]
        self.collector = MarketDataCollector(self.store, slow_bridge, bar_symbols=lambda: list(SYMBOLS),
                                             clock=lambda: self.now["t"])

    def test_heartbeat_advances_during_a_long_pass(self):
        self.collector.tick()                               # 62 x 40 s = ~41 minutes
        beats = [w["process_heartbeat_at"] for w in self.writes]
        self.assertEqual(beats, sorted(beats))
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        self.assertLessEqual(max(gaps), CALL_SECONDS)       # never older than one bridge call
        self.assertGreater(beats[-1] - beats[0], 40 * 60)

    def test_pass_started_at_is_stable_during_the_pass(self):
        self.collector.tick()
        self.assertEqual({w["pass_started_at"] for w in self.writes}, {START + 3.0})
        self.assertTrue(all(w["in_pass"] for w in self.writes[:-1]))
        self.assertFalse(self.writes[-1]["in_pass"])

    def test_last_progress_at_advances_as_symbols_complete(self):
        self.collector.tick()
        progress = [w["last_progress_at"] for w in self.writes if w["last_progress_at"] is not None]
        self.assertGreater(len(set(progress)), 50)
        self.assertEqual(progress, sorted(progress))
        counts = [w["symbols_completed_in_pass"] for w in self.writes]
        self.assertEqual(max(counts), 62)
        self.assertEqual({w["symbols_total_in_pass"] for w in self.writes if w["in_pass"]}, {62})

    def test_last_completed_pass_at_changes_only_when_the_pass_completes(self):
        self.collector.tick()
        first = self.writes[-1]["last_completed_pass_at"]
        self.assertTrue(all(w["last_completed_pass_at"] is None for w in self.writes[:-1]))
        self.assertIsNotNone(first)
        mark = len(self.writes)
        self.now["t"] += 300                               # next bar close: a second pass
        self.collector.tick()
        self.assertTrue(all(w["last_completed_pass_at"] == first for w in self.writes[mark:-1]))
        self.assertGreater(self.writes[-1]["last_completed_pass_at"], first)

    def test_probe_separates_process_liveness_from_data_health(self):
        from market_data_cache.health import evaluate
        self.collector.tick()
        mid_pass = self.writes[len(self.writes) // 2]
        during = evaluate(mid_pass, now=mid_pass["process_heartbeat_at"] + 30)
        self.assertEqual(during["process_health"], "HEALTHY")    # a slow catch-up pass is not a stall
        self.assertTrue(during["in_pass"])
        after = evaluate(self.store.health(), now=self.now["t"] + 10)
        self.assertEqual((after["process_health"], after["data_health"]), ("HEALTHY", "DEGRADED"))
        self.assertEqual(after["unhealthy_symbols"], 1)
        stalled = evaluate(self.store.health(), now=self.now["t"] + 181)
        self.assertEqual(stalled["process_health"], "STALLED")
        self.assertEqual(evaluate(None, now=0)["process_health"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
