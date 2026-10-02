import datetime
import unittest

from market_data_cache.collector import MarketDataCollector
from market_data_cache.session_calendar import classify_missing, gap_payload
from market_data_cache.store import unexpected_missing_times


class MarketDataSessionGapTests(unittest.TestCase):
    @staticmethod
    def rows(start, end, omitted=()):
        omitted = set(omitted)
        return [{"time": timestamp} for timestamp in range(start, end + 1, 900)
                if timestamp not in omitted]

    def test_daily_rollover_is_not_marked_as_a_recoverable_gap(self):
        start = int(datetime.datetime(2026, 9, 25, 20, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 26, 0, 30, tzinfo=datetime.timezone.utc).timestamp())
        omitted = {int(datetime.datetime(2026, 9, 25, 23, minute, tzinfo=datetime.timezone.utc).timestamp())
                   for minute in range(0, 60, 15)}
        rows = [{"time": timestamp} for timestamp in range(start, end + 1, 900) if timestamp not in omitted]
        self.assertEqual(unexpected_missing_times(rows, "M15"), [])

    def test_expected_gap_is_grouped_with_exact_evidence(self):
        start = int(datetime.datetime(2026, 9, 25, 22, 45, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 26, 0, 15, tzinfo=datetime.timezone.utc).timestamp())
        rows = self.rows(start, end, range(start + 900, start + 900 * 5, 900))
        expected, true = classify_missing(rows, "M15", provider_symbol="EURUSDm")
        self.assertEqual(true, [])
        self.assertEqual(len(expected), 1)
        self.assertEqual(gap_payload(expected[0], "M15"), {
            "gap_start": int(datetime.datetime(2026, 9, 25, 23, 0, tzinfo=datetime.timezone.utc).timestamp()),
            "gap_end": int(datetime.datetime(2026, 9, 26, 0, 0, tzinfo=datetime.timezone.utc).timestamp()),
            "missing_bar_count": 4,
            "previous_bar_timestamp": start,
            "next_bar_timestamp": int(datetime.datetime(2026, 9, 26, 0, 0, tzinfo=datetime.timezone.utc).timestamp()),
            "gap_duration_seconds": 3600,
        })

    def test_weekend_closure_is_valid(self):
        start = int(datetime.datetime(2026, 9, 25, 22, 45, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 27, 23, 45, tzinfo=datetime.timezone.utc).timestamp())
        rows = self.rows(start, end, range(start + 900, end, 900))
        self.assertEqual(unexpected_missing_times(rows, "M15"), [])

    def test_open_session_missing_bar_remains_true_gap(self):
        start = int(datetime.datetime(2026, 9, 28, 9, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc).timestamp())
        missing = int(datetime.datetime(2026, 9, 28, 9, 30, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(unexpected_missing_times(self.rows(start, end, [missing]), "M15"), [missing])

    def test_expected_closure_plus_true_gap_is_invalid(self):
        # Window spans a mid-session true gap at 14:30 UTC (active London/NY hours)
        # and the broker's daily session-close window at 21:00-23:00 UTC.
        # 22:30 UTC is after Exness market close (17:00 ET = 21:00 UTC EDT) so it
        # is now an expected closure, not a true gap — use 14:30 UTC instead.
        start = int(datetime.datetime(2026, 9, 25, 14, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 26, 1, 0, tzinfo=datetime.timezone.utc).timestamp())
        true_gap = int(datetime.datetime(2026, 9, 25, 14, 30, tzinfo=datetime.timezone.utc).timestamp())
        omitted = [true_gap] + list(range(int(datetime.datetime(2026, 9, 25, 21, 0,
                                                               tzinfo=datetime.timezone.utc).timestamp()),
                                          int(datetime.datetime(2026, 9, 26, 0, 0,
                                                               tzinfo=datetime.timezone.utc).timestamp()), 900))
        expected, true = classify_missing(self.rows(start, end, omitted), "M15", provider_symbol="XAUUSDm")
        self.assertTrue(expected)
        self.assertEqual([gap.start for gap in true], [true_gap])

    def test_btc_continuous_history_is_unaffected(self):
        start = int(datetime.datetime(2026, 9, 28, 9, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 28, 10, 0, tzinfo=datetime.timezone.utc).timestamp())
        expected, true = classify_missing(self.rows(start, end), "M15", provider="BTC")
        self.assertEqual(expected, [])
        self.assertEqual(true, [])

    def test_identical_recovery_is_backed_off(self):
        class Store:
            def __init__(self):
                self.values = {"recovery_next_attempt_at": 110.0}
            def state(self, _symbol):
                return dict(self.values)

        collector = MarketDataCollector(Store(), lambda *_: None, bar_symbols=lambda: [])
        self.assertFalse(collector.bars_due("EURUSDm", 109.0))
        self.assertTrue(collector.bars_due("EURUSDm", 110.0))

    def test_range_recovery_advances_over_expected_closure_without_synthetic_bars(self):
        class Store:
            def __init__(self):
                self.states = []
            def set_state(self, _symbol, value):
                self.states.append(value)

        cached = [{"time": int(datetime.datetime(2026, 9, 25, 22, 45,
                                                   tzinfo=datetime.timezone.utc).timestamp())}]
        fresh = [{"time": int(datetime.datetime(2026, 9, 26, 0, 15,
                                                  tzinfo=datetime.timezone.utc).timestamp())}]
        recovered = [{"time": int(datetime.datetime(2026, 9, 26, hour, minute,
                                                       tzinfo=datetime.timezone.utc).timestamp())}
                     for hour, minute in ((0, 0), (0, 15))]
        store = Store()
        collector = MarketDataCollector(store, lambda *_: {"rates": recovered}, bar_symbols=lambda: [])
        merged, recovery = collector._recover_range("EURUSDm", "M15", cached, fresh, {})
        self.assertEqual([row["time"] for row in merged], [cached[0]["time"], *[row["time"] for row in recovered]])
        self.assertEqual(recovery["continuity_status"], "HEALTHY")
        self.assertEqual(recovery["expected_session_closures"][0]["missing_bar_count"], 4)
        self.assertNotIn("synthetic", str(merged).lower())

    def test_range_recovery_does_not_accept_true_gap(self):
        class Store:
            def set_state(self, _symbol, _value):
                pass

        stamp = lambda hour, minute: int(datetime.datetime(2026, 9, 28, hour, minute,
                                                            tzinfo=datetime.timezone.utc).timestamp())
        cached, fresh = [{"time": stamp(10, 0)}], [{"time": stamp(11, 0)}]
        recovered = [{"time": stamp(10, 15)}, {"time": stamp(10, 45)}]
        collector = MarketDataCollector(Store(), lambda *_: {"rates": recovered}, bar_symbols=lambda: [])
        with self.assertRaises(Exception):
            collector._recover_range("EURUSDm", "M15", cached, fresh, {})


if __name__ == "__main__":
    unittest.main()
