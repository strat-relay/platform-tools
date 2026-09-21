import unittest

from .phase5b import classify_zone_interaction


class Phase5BTests(unittest.TestCase):
    def test_hover_remains_same_opportunity(self):
        bars = [{"time": 1, "low": 99.9, "high": 100.1, "close": 100.0}, {"time": 2, "low": 99.8, "high": 100.2, "close": 100.1}, {"time": 3, "low": 99.9, "high": 100.1, "close": 99.95}]
        self.assertEqual(classify_zone_interaction(bars, 99.8, 100.2)["classification"], "SAME_OPPORTUNITY_HOVER")

    def test_leave_close_outside_then_return_is_new_opportunity(self):
        bars = [{"time": 1, "low": 99.9, "high": 100.1, "close": 100.0}, {"time": 2, "low": 100.0, "high": 100.8, "close": 100.6}, {"time": 3, "low": 99.95, "high": 100.1, "close": 100.0}]
        result = classify_zone_interaction(bars, 99.8, 100.2)
        self.assertEqual(result["classification"], "NEW_OPPORTUNITY")
        self.assertEqual(result["outside_timestamp"], 2)

    def test_invalidated_return_is_not_reentry(self):
        bars = [{"time": 1, "low": 99.9, "high": 100.1, "close": 100.0}, {"time": 2, "low": 100.0, "high": 100.8, "close": 100.6}, {"time": 3, "low": 99.0, "high": 99.5, "close": 99.2}, {"time": 4, "low": 99.9, "high": 100.1, "close": 100.0}]
        result = classify_zone_interaction(bars, 99.8, 100.2, thesis_valid=lambda bar: bar["time"] != 3)
        self.assertEqual(result["classification"], "INVALIDATED_NO_REENTRY")


if __name__ == "__main__":
    unittest.main()

