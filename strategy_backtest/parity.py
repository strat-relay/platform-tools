from __future__ import annotations

from dataclasses import asdict

from .feeds import HistoricalMarketFeed
from .models import EntrySignal, SetupLifecycleEvent, StrategyVersion
from .registry import StrategyEvaluatorRegistry


def evaluate_sequential(strategy_version: StrategyVersion, parameter_set, feed: HistoricalMarketFeed, registry: StrategyEvaluatorRegistry) -> tuple:
    evaluator = registry.resolve(strategy_version)
    evaluator.initialize(strategy_version, parameter_set)
    outputs = []
    for event in feed:
        outputs.extend(evaluator.consume_market_event(event))
    return tuple(outputs)


def assert_live_replay_parity(strategy_version: StrategyVersion, parameter_set, historical_feed: HistoricalMarketFeed, live_feed: HistoricalMarketFeed, registry: StrategyEvaluatorRegistry) -> None:
    left = evaluate_sequential(strategy_version, parameter_set, historical_feed, registry)
    right = evaluate_sequential(strategy_version, parameter_set, live_feed, registry)
    left_payload = [asdict(item) for item in left if isinstance(item, (SetupLifecycleEvent, EntrySignal))]
    right_payload = [asdict(item) for item in right if isinstance(item, (SetupLifecycleEvent, EntrySignal))]
    if left_payload != right_payload:
        raise AssertionError({"historical": left_payload, "live": right_payload})
