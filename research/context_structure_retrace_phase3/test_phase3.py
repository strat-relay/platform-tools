import unittest

from .phase3 import Phase3Config, _entry_signals, _outcome, phase2_hash


class Phase3Tests(unittest.TestCase):
    def test_phase2_hash_exists(self):
        self.assertEqual(len(phase2_hash()), 64)

    def test_retracement_cannot_precede_setup(self):
        setup = {"open": 100, "high": 110, "low": 99, "close": 108}
        lower = [{"time": i * 300, "open": 100, "high": 101, "low": 99, "close": 100} for i in range(20)]
        signals = _entry_signals(lower, setup, "LONG", (0.2,), 10, 5, {"point": 0.01})
        self.assertTrue(all(s["lower_index"] >= 10 for s in signals))

    def test_invalidating_low_stops_entry_scan(self):
        setup = {"open": 100, "high": 110, "low": 99, "close": 108}
        lower = [{"time": i * 300, "open": 100, "high": 101, "low": 99, "close": 100} for i in range(20)]
        lower[11]["low"] = 98
        signals = _entry_signals(lower, setup, "LONG", (0.2,), 10, 5, {"point": 0.01})
        self.assertEqual(signals, [])

    def test_target_r_is_geometry_not_hardcoded(self):
        lower = [{"time": i * 300, "open": 102, "high": 102, "low": 102, "close": 102} for i in range(5)]
        lower[0].update({"high": 103, "low": 99, "close": 102})
        result = _outcome(lower, {"lower_index": 0, "entry_price": 100}, 98, 104, "LONG")
        self.assertEqual(result["outcome"], "TIME_EXIT")
        self.assertAlmostEqual(result["r"], 1.0)

    def test_timeframe_config_is_explicit(self):
        config = Phase3Config()
        self.assertEqual(config.timeframes.execution, "M15")
        self.assertEqual(config.timeframes.lower, ("M5",))
        self.assertEqual(config.timeframes.higher, ("H1", "H4"))


if __name__ == "__main__":
    unittest.main()
