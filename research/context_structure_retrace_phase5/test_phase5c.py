import unittest

from .phase5 import _geometry
from .phase5c import _classify


class Phase5CTests(unittest.TestCase):
    def test_long_nonpositive_target_has_no_positive_r(self):
        item = {"direction": "LONG", "entry_price": 100.0, "spread": 0.1, "stop_hypotheses": {"ORIGINATING_SETUP_EXTREME": {"stop": 98.0, "stop_distance": 2.0, "stop_distance_atr": 1.0}}, "target_hypotheses": {"CANDLE_EXTENSION_50": 99.0, "NEXT_OPPOSING_STRUCTURE": 99.5}}
        g = _geometry(item)
        self.assertLessEqual(g["target_distance"], 0)
        self.assertNotEqual(g["target_state"], "TARGET_BEYOND_ENTRY")

    def test_short_nonpositive_target_has_no_positive_r(self):
        item = {"direction": "SHORT", "entry_price": 100.0, "spread": 0.1, "stop_hypotheses": {"ORIGINATING_SETUP_EXTREME": {"stop": 102.0, "stop_distance": 2.0, "stop_distance_atr": 1.0}}, "target_hypotheses": {"CANDLE_EXTENSION_50": 101.0, "NEXT_OPPOSING_STRUCTURE": 100.5}}
        g = _geometry(item)
        self.assertLessEqual(g["target_distance"], 0)
        self.assertNotEqual(g["target_state"], "TARGET_BEYOND_ENTRY")

    def test_reentry_lifecycle_distinguishes_completion(self):
        bars = [{"time": 100, "high": 101, "low": 99, "close": 100}, {"time": 200, "high": 105, "low": 100, "close": 104}, {"time": 300, "high": 101, "low": 99, "close": 100}]
        item = {"direction": "LONG", "entry_price": 100.0, "entry_reference": 100.0, "entry_opportunity_number": 2, "potential_scale_in": True, "fill_time": 300, "lower_index": 2, "setup_timestamp": 0}
        item["target_hypotheses"] = {"CANDLE_EXTENSION_50": 104.0}
        result = _classify(item, bars)
        self.assertEqual(result["reentry_lifecycle"], "RETURN_AFTER_SETUP_TARGET_COMPLETED")


if __name__ == "__main__":
    unittest.main()

