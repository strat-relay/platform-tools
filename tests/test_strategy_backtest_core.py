import tempfile
import unittest
from pathlib import Path

from strategy_backtest import (
    BacktestArtifactStore,
    BacktestEngine,
    CostModel,
    EntrySignal,
    HistoricalMarketFeed,
    MarketEvent,
    ParameterSchema,
    ParameterSet,
    ParameterSetCatalog,
    StrategyEvaluatorRegistry,
    StrategyVersion,
    assert_live_replay_parity,
)


class ThresholdEvaluator:
    def __init__(self):
        self.threshold = 0.0
        self.emitted = False

    def initialize(self, strategy_version, parameter_set):
        self.threshold = float(parameter_set.values["threshold"])

    def consume_market_event(self, event):
        if self.emitted or event.close <= self.threshold:
            return ()
        self.emitted = True
        return (EntrySignal("SIG_TEST", "TEST_BREAKOUT@V1", event.canonical_instrument, "LONG", event.close, event.close - 1, event.close + 2, event.close_timestamp, expiry_timestamp=event.close_timestamp + 900, provenance={"available_through": event.close_timestamp, "timeframe": event.timeframe}),)

    def snapshot_state(self):
        return {"threshold": self.threshold, "emitted": self.emitted}

    def restore_state(self, state):
        self.threshold = state["threshold"]
        self.emitted = state["emitted"]


def event(index, close, *, completed=True):
    timestamp = 1_700_000_000 + index * 300
    return MarketEvent("XAUUSD", "M5", timestamp, timestamp + 300, close - 0.2, close + 0.5, close - 0.5, close, completed=completed, source="fixture", provenance={"dataset": "test"})


class BacktestCoreTests(unittest.TestCase):
    def setUp(self):
        self.schema = ParameterSchema("test-breakout-v1", {"threshold": {"required": True, "minimum": 0}})
        self.version = StrategyVersion("TEST_BREAKOUT", "V1", "threshold", self.schema, lifecycle="IMPLEMENTED")
        self.parameters = ParameterSet("test-default", "TEST_BREAKOUT@V1", "test-breakout-v1", {"threshold": 10}, {"source": "fixture"})
        self.registry = StrategyEvaluatorRegistry()
        self.registry.register("threshold", ThresholdEvaluator)

    def test_market_run_metrics_costs_and_artifact_are_deterministic(self):
        feed = HistoricalMarketFeed((event(0, 9), event(1, 11), event(2, 13), event(3, 12)), "fixture-v1", partition="DISCOVERY")
        engine = BacktestEngine(self.registry)
        first = engine.run(self.version, self.parameters, feed, CostModel("fixture-cost", spread_price=0.1), run_id="run-a")
        second = engine.run(self.version, self.parameters, feed, CostModel("fixture-cost", spread_price=0.1), run_id="run-b")
        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertEqual(first.metrics["signal_count"], 1)
        self.assertEqual(first.metrics["trade_count"], 1)
        self.assertEqual(first.outcomes[0].status, "TARGET_HIT")
        self.assertLess(first.outcomes[0].realized_r, 2.0)
        self.assertEqual(first.metrics["average_hold_duration"], 300)
        self.assertIn("M5", first.metrics["timeframe"])
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(BacktestArtifactStore(Path(directory)).write(first).exists())

    def test_completed_only_and_chronological_feed(self):
        feed = HistoricalMarketFeed((event(0, 9, completed=False), event(1, 9)), "fixture-v1")
        self.assertEqual(len(feed.events), 1)
        with self.assertRaises(ValueError):
            HistoricalMarketFeed((event(1, 9), event(0, 9)), "fixture-v1")

    def test_prefix_invariance_and_future_mutation(self):
        prefix = HistoricalMarketFeed(tuple(event(i, 9 if i < 2 else 11) for i in range(3)), "prefix")
        longer = HistoricalMarketFeed(tuple(event(i, 9 if i < 2 else 11) for i in range(6)), "longer")
        mutated = HistoricalMarketFeed(tuple(event(i, 9 if i < 2 else (11 if i < 3 else 999)) for i in range(6)), "mutated")
        engine = BacktestEngine(self.registry)
        self.assertEqual([x.signal_id for x in engine.run(self.version, self.parameters, prefix, CostModel("zero")).signals], [x.signal_id for x in engine.run(self.version, self.parameters, longer, CostModel("zero")).signals])
        self.assertEqual([x.entry_price for x in engine.run(self.version, self.parameters, longer, CostModel("zero")).signals], [x.entry_price for x in engine.run(self.version, self.parameters, mutated, CostModel("zero")).signals])
        self.assertEqual(engine.run(self.version, self.parameters, prefix, CostModel("zero")).signals[0].provenance["available_through"], prefix.events[2].close_timestamp)

    def test_same_bar_stop_target_is_conservative_stop_first(self):
        feed = HistoricalMarketFeed((event(0, 9), event(1, 11), MarketEvent("XAUUSD", "M5", 1_700_000_600, 1_700_000_900, 11, 14, 9, 12)), "fixture")
        result = BacktestEngine(self.registry).run(self.version, self.parameters, feed, CostModel("zero"))
        self.assertEqual(result.outcomes[0].status, "STOPPED")
        self.assertEqual(result.outcomes[0].reason, "SAME_BAR_STOP_AND_TARGET")

    def test_parameter_schema_and_validated_immutability(self):
        self.schema.validate({"threshold": 10})
        with self.assertRaises(ValueError):
            self.schema.validate({"threshold": -1})
        catalog = ParameterSetCatalog()
        catalog.add(self.parameters)
        catalog.mark_authoritative(self.parameters)
        changed = ParameterSet("test-default", "TEST_BREAKOUT@V1", "test-breakout-v1", {"threshold": 11})
        with self.assertRaises(ValueError):
            catalog.add(changed)

    def test_parity_registry_and_dataset_fingerprint(self):
        events = tuple(event(i, 9 if i < 2 else 11) for i in range(4))
        historical = HistoricalMarketFeed(events, "dataset", partition="VALIDATION")
        live = historical.with_source("live-sequential")
        assert_live_replay_parity(self.version, self.parameters, historical, live, self.registry)
        self.assertEqual(historical.snapshot_id, live.snapshot_id)
        self.assertNotEqual(historical.dataset_fingerprint, live.dataset_fingerprint)
        self.assertEqual(historical.partition, "VALIDATION")


if __name__ == "__main__":
    unittest.main()
