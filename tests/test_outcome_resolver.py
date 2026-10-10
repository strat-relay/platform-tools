from datetime import datetime, timedelta, timezone
import unittest

from outcome_resolver import Candle, EvaluationContract, resolve_candle_path


UTC = timezone.utc
START = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class OutcomeResolverTests(unittest.TestCase):
    def candle(self, offset: int, *, high: float, low: float) -> Candle:
        opened = START + timedelta(minutes=offset)
        return Candle(opened, opened + timedelta(minutes=5), high, low)

    def test_replay_resolves_first_provable_target(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START,
            candles=[self.candle(0, high=100.4, low=99.8), self.candle(5, high=101.2, low=100.1)],
        )
        self.assertEqual(result.status, "TARGET_HIT")
        self.assertEqual(result.resolution_state, "RESOLVED")

    def test_replay_does_not_invent_intrabar_order(self):
        result = resolve_candle_path(
            direction="SHORT", entry=100, stop=101, target=99,
            entry_timestamp=START,
            candles=[self.candle(0, high=101.2, low=98.8)],
        )
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.resolution_state, "AMBIGUOUS_INTRABAR")
        self.assertIsNone(result.realized_r)

    def test_replay_reports_insufficient_coverage(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START,
            candles=[self.candle(0, high=100.4, low=99.8)],
        )
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.resolution_state, "INSUFFICIENT_DATA")

    def test_same_candle_entry_activation_and_exit_is_not_invented(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=102,
            entry_timestamp=START, activation="ENTRY_PRICE_TOUCH",
            candles=[Candle(START, START + timedelta(minutes=15), 103, 98, 101)],
        )
        self.assertEqual(result.resolution_state, "AMBIGUOUS_INTRABAR")

    def test_time_exit_uses_completed_candle_close_not_midpoint(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=110,
            entry_timestamp=START, max_hold_minutes=15,
            candles=[Candle(START, START + timedelta(minutes=15), 105, 100.5, 103)],
        )
        self.assertEqual(result.status, "TIME_EXIT")
        self.assertEqual(result.realized_r, 3)
        self.assertEqual(result.evidence["price_source"], "CANDLE_CLOSE")

    def test_contract_is_signal_versioned_and_strategy_agnostic(self):
        contract = EvaluationContract.from_signal({
            "entry_type": "LIMIT",
            "strategy_metadata": {"outcome_contract": {
                "version": "entry-outcome.v3", "activation": "ENTRY_PRICE_TOUCH",
                "expiration_minutes": 30}},
        })
        self.assertEqual(contract.version, "entry-outcome.v3")
        self.assertEqual(contract.activation, "ENTRY_PRICE_TOUCH")
        self.assertEqual(contract.expiration_minutes, 30)


if __name__ == "__main__":
    unittest.main()
