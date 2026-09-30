import datetime
import unittest

from market_data_cache.store import unexpected_missing_times


class MarketDataSessionGapTests(unittest.TestCase):
    def test_daily_rollover_is_not_marked_as_a_recoverable_gap(self):
        start = int(datetime.datetime(2026, 9, 25, 20, 0, tzinfo=datetime.timezone.utc).timestamp())
        end = int(datetime.datetime(2026, 9, 26, 0, 30, tzinfo=datetime.timezone.utc).timestamp())
        omitted = {int(datetime.datetime(2026, 9, 25, hour, minute, tzinfo=datetime.timezone.utc).timestamp())
                   for hour in (21, 22, 23) for minute in range(0, 60, 15)}
        rows = [{"time": timestamp} for timestamp in range(start, end + 1, 900) if timestamp not in omitted]
        self.assertEqual(unexpected_missing_times(rows, "M15"), [])


if __name__ == "__main__":
    unittest.main()
