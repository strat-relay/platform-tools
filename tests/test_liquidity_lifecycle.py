import unittest

from liquidity_lifecycle import settle_filled_entry


class LiquidityLifecycleTests(unittest.TestCase):
    def test_same_bar_stop_precedes_target(self):
        outcome = settle_filled_entry(
            direction="LONG", entry=100, stop=99, target=101, fill_timestamp=1000,
            bars=[{"time": 1000, "low": 98, "high": 102, "close": 100}], max_hold_minutes=120)
        self.assertEqual(outcome.status, "STOPPED")

    def test_target_is_canonical_target_hit(self):
        outcome = settle_filled_entry(
            direction="LONG", entry=100, stop=99, target=101, fill_timestamp=1000,
            bars=[{"time": 1000, "low": 100, "high": 101, "close": 101}], max_hold_minutes=120)
        self.assertEqual((outcome.status, outcome.realized_r), ("TARGET_HIT", 1.0))

    def test_bounded_hold_is_time_exit_not_stop_or_target(self):
        outcome = settle_filled_entry(
            direction="LONG", entry=100, stop=99, target=101, fill_timestamp=1000,
            bars=[{"time": 1000 + 120 * 60 - 300, "low": 99, "high": 101, "close": 100.25}], max_hold_minutes=120)
        self.assertEqual(outcome.status, "TIME_EXIT")
        self.assertEqual(outcome.realized_r, 0.25)

    def test_open_entry_has_no_terminal_outcome(self):
        self.assertIsNone(settle_filled_entry(
            direction="SHORT", entry=100, stop=101, target=98.75, fill_timestamp=1000,
            bars=[{"time": 1000, "low": 99.5, "high": 100.5, "close": 100}], max_hold_minutes=120))


if __name__ == "__main__":
    unittest.main()
