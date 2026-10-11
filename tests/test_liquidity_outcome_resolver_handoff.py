import unittest
from datetime import datetime, timezone

from liquidity_lifecycle import settle_filled_entry
from orchestration.liquidity_instances import (LIQUIDITY_INSTANCE_DEFINITIONS,
                                                LiquidityLivePublisher)
from outcome_resolver import Candle, EvaluationContract, resolve_candle_path
from scripts.audit_outcome_parity import contract_gap


class LiquidityOutcomeResolverHandoffTests(unittest.TestCase):
    def candle(self, *, high, low, close, minute=5):
        opened = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
        return Candle(opened, opened.replace(minute=opened.minute + 5), high, low, close)

    def test_liquidity_legacy_stop_first_semantics_are_representable(self):
        contract = EvaluationContract(
            version="entry-outcome.v2", timeframe_minutes=5,
            activation="SIGNAL_TIMESTAMP", max_hold_minutes=120,
            time_exit_price="CLOSE", same_candle_priority="STOP_FIRST",
            time_exit_priority="BEFORE_PRICE")
        bars = [self.candle(high=102, low=98, close=100)]
        resolved = resolve_candle_path(
            direction="LONG", entry=100, stop=99, target=101,
            entry_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            candles=bars, max_hold_minutes=contract.max_hold_minutes,
            timeframe_minutes=contract.timeframe_minutes,
            activation=contract.activation,
            time_exit_priority=contract.time_exit_priority,
            same_candle_priority=contract.same_candle_priority)
        legacy = settle_filled_entry(
            direction="LONG", entry=100, stop=99, target=101,
            fill_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bars=[{"open": 100, "high": 102, "low": 98, "close": 100,
                   "time": "2026-01-01T00:00:00+00:00"}],
            max_hold_minutes=120)
        self.assertEqual(resolved.status, "STOPPED")
        self.assertEqual(legacy.status, "STOPPED")

    def test_current_liquidity_publisher_is_rejected_until_contract_is_added(self):
        definition = LIQUIDITY_INSTANCE_DEFINITIONS[0]
        definition = definition.__class__(**{
            **definition.__dict__, "real_execution_enabled": True})
        publisher = LiquidityLivePublisher(definition, mode="REAL_ELIGIBLE")
        signal = publisher.publish({
            "setup_id": "setup-1", "event_time": "2026-01-01T00:00:00+00:00",
            "decision_time": "2026-01-01T00:00:00+00:00", "entry": 100,
            "stop": 99, "target": 101, "direction": "LONG",
            "source_event_id": "event-1", "source_health": True,
            "gap_recovery": False}, emitted_at="2026-01-01T00:00:01+00:00")
        self.assertIsNotNone(signal)
        gaps = contract_gap(signal.to_dict())
        self.assertIn("strategy_metadata.outcome_contract", gaps)
        self.assertIsNone(signal.economic_position_id)
        self.assertEqual(signal.entry_opportunity_id, "setup-1")


if __name__ == "__main__":
    unittest.main()
