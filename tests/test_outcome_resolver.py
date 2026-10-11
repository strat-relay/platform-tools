from datetime import datetime, timedelta, timezone
import unittest

from outcome_resolver import Candle, EvaluationContract, economic_position_groups, resolve_candle_path


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

    def test_replay_rejects_cache_that_starts_after_signal_coverage(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START,
            candles=[self.candle(30, high=101.2, low=100.2)],
        )
        self.assertEqual(result.resolution_state, "INSUFFICIENT_DATA")
        self.assertEqual(result.evidence["reason"], "CANDLE_GAP")

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

    def test_liquidity_contract_can_preserve_stop_first_same_candle_semantics(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START, same_candle_priority="STOP_FIRST",
            candles=[self.candle(0, high=102, low=98)],
        )
        self.assertEqual(result.status, "STOPPED")

    def test_liquidity_contract_can_prioritize_time_exit_before_price(self):
        result = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START, max_hold_minutes=15,
            time_exit_priority="BEFORE_PRICE",
            candles=[Candle(START, START + timedelta(minutes=15), 102, 98, 100.5)],
        )
        self.assertEqual(result.status, "TIME_EXIT")
        self.assertEqual(result.exit_price, 100.5)

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

    def test_executable_long_requires_bid_ask_and_uses_bid_for_exit(self):
        missing = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START, price_basis="EXECUTABLE_BID_ASK",
            candles=[Candle(START, START + timedelta(minutes=15), 101, 99, 100.5)],
        )
        self.assertEqual(missing.resolution_state, "INSUFFICIENT_DATA")
        self.assertEqual(missing.evidence["reason"], "MISSING_EXECUTABLE_QUOTES")
        resolved = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=START, price_basis="EXECUTABLE_BID_ASK",
            candles=[Candle(START, START + timedelta(minutes=15), 101, 99, 100.5,
                            bid_high=101.1, bid_low=100.1,
                            ask_high=101.2, ask_low=100.2, bid_close=100.5)],
        )
        self.assertEqual(resolved.status, "TARGET_HIT")
        self.assertEqual(resolved.price_basis, "EXECUTABLE_BID_ASK")
        self.assertEqual(resolved.exit_price, 101)

    def test_spread_does_not_synthesize_quotes(self):
        result = resolve_candle_path(
            direction="SHORT", entry=100, stop=101, target=99,
            entry_timestamp=START, price_basis="EXECUTABLE_BID_ASK",
            candles=[Candle(START, START + timedelta(minutes=15), 101, 99, 100,
                            spread=0.2)],
        )
        self.assertEqual(result.resolution_state, "INSUFFICIENT_DATA")

    def test_economic_position_reporting_preserves_signal_rows(self):
        groups = economic_position_groups([
            {"signal_id": "A", "economic_position_id": "P1"},
            {"signal_id": "B", "economic_position_id": "P1"},
            {"signal_id": "C"},
            {"signal_id": "D", "instrument": "EURUSD", "direction": "LONG",
             "entry_price": 1.1, "stop_price": 1.0, "target_price": 1.2,
             "decision_time": START},
            {"signal_id": "E", "instrument": "EURUSD", "direction": "LONG",
             "entry_price": 1.1, "stop_price": 1.0, "target_price": 1.2,
             "decision_time": START},
        ])
        position = next(group for group in groups if group["group_id"] == "P1")
        self.assertEqual(position["signal_count"], 2)
        self.assertEqual(position["signal_ids"], ["A", "B"])
        self.assertEqual(next(group for group in groups if "C" in group["signal_ids"])["group_type"], "INDEPENDENT_SIGNAL")
        inferred = next(group for group in groups if "D" in group["signal_ids"])
        self.assertEqual(inferred["group_type"], "INFERRED_ECONOMIC_SIGNATURE")
        self.assertFalse(inferred["identity_authoritative"])


if __name__ == "__main__":
    unittest.main()
