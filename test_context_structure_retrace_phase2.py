from __future__ import annotations

import copy
import unittest

from context_structure_retrace.attention import attention_layer, group_channels
from context_structure_retrace.data import CausalReplay, bar_end
from context_structure_retrace.ledger import build_phase2_ledger
from context_structure_retrace.outcomes import future_outcome_labels
from context_structure_retrace.replay import feature_snapshot
from context_structure_retrace.retracement import measure_retracement
from context_structure_retrace.structures import meaningful_interaction_episodes


def bar(ts, open_, high, low, close):
    return {"time": ts, "open": open_, "high": high, "low": low, "close": close, "spread": 2, "tick_volume": 1}


class Phase2RepresentationTests(unittest.TestCase):
    def setUp(self):
        self.m5 = [bar(i * 300, 100 + i * .2, 101 + i * .2, 99 + i * .2, 100.5 + i * .2) for i in range(40)]
        self.m15 = [bar(i * 900, 100 + i * .6, 103 + i * .6, 97 + i * .6, 102 + i * .6) for i in range(20)]
        self.h1 = [bar(i * 3600, 100 + i * 2.4, 112 + i * 2.4, 88 + i * 2.4, 108 + i * 2.4) for i in range(8)]
        self.h4 = [bar(i * 14400, 100 + i * 9.6, 148 + i * 9.6, 52 + i * 9.6, 140 + i * 9.6) for i in range(3)]
        self.replay = CausalReplay({"M5": self.m5, "M15": self.m15, "H1": self.h1, "H4": self.h4})

    def test_attention_does_not_depend_on_future_labels(self):
        as_of = bar_end(self.m5[-1], "M5")
        snapshot = feature_snapshot(self.replay, "TEST", as_of)
        before = attention_layer(snapshot)
        snapshot_with_labels = copy.deepcopy(snapshot)
        snapshot_with_labels["future_outcome_labels"] = {"24": {"net": 999999}}
        self.assertEqual(before, attention_layer(snapshot_with_labels))

    def test_normalized_context_is_stable_under_price_scale(self):
        scaled = {tf: [{**x, **{k: float(x[k]) * 100.0 for k in ("open", "high", "low", "close")}} for x in bars] for tf, bars in self.replay.bars_by_timeframe.items()}
        original = feature_snapshot(self.replay, "TEST", bar_end(self.m5[-1], "M5"))
        scaled_replay = CausalReplay(scaled)
        scaled_snapshot = feature_snapshot(scaled_replay, "TEST", bar_end(scaled["M5"][-1], "M5"))
        oema = original["timeframes"]["M15"]["ema_context"]
        sema = scaled_snapshot["timeframes"]["M15"]["ema_context"]
        self.assertEqual(oema["ordering"], sema["ordering"])
        for key, value in oema["normalized_price_distance_atr"].items():
            if value is not None:
                self.assertAlmostEqual(value, sema["normalized_price_distance_atr"][key], places=6)

    def test_timeframe_wiring_is_caller_configurable(self):
        context = self.replay.synchronized_context(bar_end(self.m5[-1], "M5"), ("M5", "H1"))
        self.assertEqual(set(context), {"M5", "H1"})

    def test_channel_grouping_is_deterministic_and_atr_relative(self):
        candidates = [
            {"structure_id": "B", "current_upper": 110.2, "current_lower": 100.2, "slope_price_per_minute": .1, "slope_atr_per_hour": .2, "touches_upper": 2, "touches_lower": 2, "violations": []},
            {"structure_id": "A", "current_upper": 110.0, "current_lower": 100.0, "slope_price_per_minute": .1, "slope_atr_per_hour": .2, "touches_upper": 2, "touches_lower": 2, "violations": []},
        ]
        first = group_channels(candidates, 1.0)
        second = group_channels(list(reversed(candidates)), 1.0)
        self.assertEqual(first, second)
        self.assertEqual(first["raw_count"], 2)
        self.assertEqual(first["cluster_count"], 1)

    def test_trendline_interactions_need_departure_before_return(self):
        episodes = meaningful_interaction_episodes([0.1, 0.2, 0.3, 2.5, 2.7, 0.2, 0.1, 0.2], list(range(8)), 1.0)
        self.assertEqual(len(episodes), 1)
        hovering = meaningful_interaction_episodes([0.1, 0.2, 0.3, 0.4, 0.2], list(range(5)), 1.0)
        self.assertEqual(hovering, [])

    def test_retracement_is_causal_to_supplied_future_window(self):
        event = bar(0, 100, 110, 98, 108)
        future = [bar(300, 108, 112, 105, 110)]
        a = measure_retracement(event, future, "UP", 4.0)
        b = measure_retracement(event, [bar(300, 108, 109, 105, 108)] + [bar(600, 108, 109, 90, 108)], "UP", 4.0)
        self.assertNotEqual(a["maximum_retracement_price"], b["maximum_retracement_price"])
        # The function is causal to the exact supplied window; the caller decides
        # which horizon is a label, and does not pass it to feature generation.
        self.assertTrue(a["provenance"]["not_available_to_feature_generation"])

    def test_outcome_labels_are_separate_from_features(self):
        event = bar(0, 100, 110, 98, 108)
        labels = future_outcome_labels(event, [bar(300, 108, 112, 105, 110)], "UP", 4.0)
        self.assertIn("provenance", labels)
        self.assertTrue(labels["provenance"]["leakage_guard"])

    def test_ledger_is_causal_and_marks_labels_separately(self):
        rows = list(build_phase2_ledger(self.replay, "TEST", 0, bar_end(self.m5[-1], "M5")))
        for row in rows:
            self.assertTrue(row["ledger_provenance"]["features_and_attention_causal"])
            self.assertTrue(row["ledger_provenance"]["future_labels_separate"])
            self.assertTrue(row["context_snapshot"]["provenance"]["future_ohlc_exposed"] is False)


if __name__ == "__main__":
    unittest.main()
