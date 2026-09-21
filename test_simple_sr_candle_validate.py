import unittest

import simple_sr_candle_validate as s


class SimpleSRCandleTests(unittest.TestCase):
    def candle(self, o, h, l, c, t):
        return {"open": o, "high": h, "low": l, "close": c, "time": t}

    def test_bullish_engulfing_is_body_only(self):
        bars = [self.candle(101, 102, 99, 100, 0), self.candle(102, 103, 99.5, 100, 300), self.candle(100, 104, 98, 103, 600)]
        patterns = s.candle_pattern(bars, 2)
        self.assertEqual(patterns[0][0], "BULLISH_ENGULFING")

    def test_evening_star_is_information_only(self):
        bars = [self.candle(100, 103, 99, 102, 0), self.candle(102, 103, 101.5, 102.1, 300), self.candle(102, 102.2, 98, 99, 600)]
        self.assertTrue(any(p[0] == "EVENING_STAR_INFO" for p in s.candle_pattern(bars, 2)))

    def test_target_outcome_prioritizes_stop_same_bar(self):
        row = {"entry": 100.0, "stop_loss": 99.0}
        bars = [{"open": 100, "high": 101.3, "low": 98.9, "close": 100.2, "time": 0}]
        result = s.settle(row, bars, 0, 1.25)
        self.assertEqual(result["outcome"], "LOSS")
        self.assertEqual(result["exit_reason"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
