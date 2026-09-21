from __future__ import annotations

import copy
import unittest

from context_structure_retrace.data import CausalReplay, bar_end
from context_structure_retrace.indicators import ema, ema_context
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.replay import feature_snapshot
from context_structure_retrace.sr import confirmed_swings, sr_context


def bar(ts, open_, high, low, close, spread=2):
    return {"time": ts, "open": open_, "high": high, "low": low, "close": close, "spread": spread, "tick_volume": 1}


class Phase1CausalReplayTests(unittest.TestCase):
    def setUp(self):
        self.m5 = [bar(i * 300, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        self.m15 = [bar(i * 900, 100 + i * 3, 103 + i * 3, 97 + i * 3, 102 + i * 3) for i in range(12)]
        self.h1 = [bar(i * 3600, 100 + i * 12, 112 + i * 12, 88 + i * 12, 108 + i * 12) for i in range(4)]
        self.h4 = [bar(i * 14400, 100 + i * 48, 148 + i * 48, 52 + i * 48, 140 + i * 48) for i in range(2)]
        self.replay = CausalReplay({"M5": self.m5, "M15": self.m15, "H1": self.h1, "H4": self.h4})

    def test_forming_htf_excludes_future_ohlc(self):
        as_of = 3600 + 600
        view = self.replay.view("H1", as_of)
        self.assertEqual(view["completed"]["time"], 0)
        self.assertIsNotNone(view["forming"])
        self.assertEqual(view["forming"]["source_bar_count"], 2)
        self.assertLess(view["forming"]["high"], 112 + 12)
        self.assertTrue(view["provenance"]["future_bars_excluded"])

    def test_future_bar_mutation_does_not_change_snapshot(self):
        as_of = 3600 + 600
        before = self.replay.view("H1", as_of)
        mutated = copy.deepcopy(self.h1)
        mutated[2]["high"] = 999999
        after = CausalReplay({"M5": self.m5, "M15": self.m15, "H1": mutated, "H4": self.h4}).view("H1", as_of)
        self.assertEqual(before, after)

    def test_swing_is_available_only_after_confirmation(self):
        bars = [
            bar(0, 10, 10, 9, 9.5), bar(300, 10, 12, 9, 11), bar(600, 11, 15, 10, 14),
            bar(900, 14, 13, 8, 9), bar(1200, 9, 11, 8, 10), bar(1500, 10, 10, 7, 8),
        ]
        early = confirmed_swings(bars, "M5", 1200, lookback=2)
        late = confirmed_swings(bars, "M5", 1800, lookback=2)
        self.assertEqual(early, [])
        self.assertTrue(any(x["type"] == "RESISTANCE" and x["timestamp"] == 600 for x in late))

    def test_ema_values_and_cross_timing_are_deterministic(self):
        values = ema([1, 2, 3], 2)
        self.assertAlmostEqual(values[-1], 2.5555555555)
        first = ema_context(self.m5, None, bar_end(self.m5[-1], "M5"), "M5")
        second = ema_context(self.m5, None, bar_end(self.m5[-1], "M5"), "M5")
        self.assertEqual(first, second)
        self.assertIn("crosses", first)
        self.assertIn("ordering", first)

    def test_pattern_requires_completed_candle(self):
        bars = [bar(0, 10, 11, 9, 9), bar(300, 9, 10, 8, 8), bar(600, 8, 12, 7, 11)]
        self.assertEqual(detect_patterns(bars, "M5", 600, "TEST"), [])
        events = detect_patterns(bars, "M5", 900, "TEST")
        self.assertTrue(any(x["pattern"] == "BULLISH_ENGULFING" for x in events))

    def test_sr_future_swing_not_available(self):
        bars = [
            bar(0, 100, 101, 99, 100), bar(900, 100, 102, 98, 101),
            bar(1800, 101, 110, 100, 109), bar(2700, 109, 103, 97, 99),
            bar(3600, 99, 102, 96, 100), bar(4500, 100, 101, 95, 99),
            bar(5400, 99, 100, 94, 98), bar(6300, 98, 99, 93, 97),
            bar(7200, 97, 98, 92, 96),
        ]
        early = sr_context(bars, "M15", 6 * 900)
        later = sr_context(bars, "M15", 9 * 900)
        self.assertTrue(early["provenance"]["future_bars_excluded"])
        self.assertNotEqual(early["zones"], later["zones"])

    def test_full_snapshot_has_all_phase1_contexts(self):
        snapshot = feature_snapshot(self.replay, "TEST", bar_end(self.m5[-1], "M5"))
        self.assertEqual(set(snapshot["timeframes"]), {"M5", "M15", "H1", "H4"})
        self.assertIn("market_snapshot", snapshot)
        self.assertIn("trendline_context", snapshot)
        self.assertIn("channel_context", snapshot)
        self.assertFalse(snapshot["provenance"]["future_ohlc_exposed"])

    def test_structure_candidates_are_bounded_and_causal(self):
        snapshot = feature_snapshot(self.replay, "TEST", bar_end(self.m5[-1], "M5"))
        for candidate in snapshot["trendline_context"] + snapshot["channel_context"]:
            self.assertTrue(candidate["provenance"]["future_bars_excluded"])


if __name__ == "__main__":
    unittest.main()
