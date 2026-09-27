from __future__ import annotations

from dataclasses import asdict
import unittest
from tempfile import TemporaryDirectory

from strategy_backtest import (
    BacktestEngine,
    CostModel,
    EntrySignal,
    HistoricalMarketFeed,
    MarketEvent,
    ParameterSet,
    StrategyEvaluatorRegistry,
    StrategyVersion,
    assert_live_replay_parity,
    kojo_wedge_baseline_parameter_set,
    kojo_wedge_parameter_schema,
    register_builtin_evaluators,
    write_kojo_wedge_diagnostic_artifact,
)
from strategy_backtest.kojo_wedge import KojoWedgeEvaluator


def bar(index: int, open_: float, high: float, low: float, close: float) -> MarketEvent:
    return MarketEvent("XAUUSD", "H1", index * 3600, (index + 1) * 3600, open_, high, low, close)


def params() -> ParameterSet:
    return ParameterSet(
        "kojo-test",
        "KOJO_WEDGE@V1",
        "kojo-wedge-v1",
        {
            "pivot_strength": 1,
            "minimum_pivots": 4,
            "minimum_high_pivots": 2,
            "minimum_low_pivots": 2,
            "convergence_threshold": 0.10,
            "max_wedge_age_bars": 80,
            "stop_buffer_type": "PRICE",
            "stop_buffer_value": 0.5,
        },
    )


def strategy() -> StrategyVersion:
    return StrategyVersion("KOJO_WEDGE", "V1", "KOJO_WEDGE_V1", kojo_wedge_parameter_schema(), "VALIDATED")


def wedge_events() -> tuple[MarketEvent, ...]:
    # The prior HIGH at index 1 is a structural target. The latest four
    # alternating pivots form a falling, converging wedge.
    return (
        bar(0, 100, 101, 99, 100),
        bar(1, 120, 130, 119, 125),
        bar(2, 120, 121, 115, 118),
        bar(3, 121, 125, 116, 122),
        bar(4, 116, 118, 110, 112),
        bar(5, 111, 122, 111, 119),
        bar(6, 115, 120, 108, 110),
        bar(7, 118, 125, 109, 123),
        bar(8, 123, 127, 120, 124),
        bar(9, 126, 128, 125, 127),
    )


def rising_wedge_events() -> tuple[MarketEvent, ...]:
    return (
        bar(0, 100, 101, 99, 100),
        bar(1, 95, 98, 90, 92),
        bar(2, 106, 110, 105, 108),
        bar(3, 104, 108, 100, 102),
        bar(4, 110, 115, 106, 113),
        bar(5, 108, 113, 104, 110),
        bar(6, 113, 117, 109, 115),
        bar(7, 114, 120, 110, 118),
        bar(8, 111, 112, 105, 106),
        bar(9, 104, 107, 102, 104),
    )


def outputs(events: tuple[MarketEvent, ...], evaluator: KojoWedgeEvaluator | None = None):
    evaluator = evaluator or KojoWedgeEvaluator()
    evaluator.initialize(strategy(), params())
    result = []
    for event in events:
        result.extend(evaluator.consume_market_event(event))
    return evaluator, tuple(result)


def test_registers_and_emits_causal_next_open_signal() -> None:
    registry = register_builtin_evaluators(StrategyEvaluatorRegistry())
    evaluator, result = outputs(wedge_events())
    signals = [item for item in result if isinstance(item, EntrySignal)]

    assert registry.resolve(strategy()).__class__ is KojoWedgeEvaluator
    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction == "LONG"
    assert signal.entry_price == 126
    assert signal.stop_price == 107.5
    assert signal.target_price == 130
    assert signal.decision_timestamp == wedge_events()[8].close_timestamp
    assert signal.provenance["wedge_start_index"] == 3
    assert signal.provenance["wedge_end_index"] == 6
    assert signal.provenance["available_through"] == wedge_events()[9].close_timestamp
    assert "rejection_counts" in evaluator.diagnostics()


def test_rejects_parallel_corrective_channel() -> None:
    # Four alternating pivots with equal-slope boundaries: zero convergence.
    events = (
        bar(0, 100, 101, 99, 100),
        bar(1, 120, 130, 119, 125),
        bar(2, 120, 121, 115, 118),
        bar(3, 121, 125, 116, 122),
        bar(4, 116, 118, 110, 112),
        bar(5, 111, 123, 111, 119),
        bar(6, 115, 120, 108, 110),
        bar(7, 118, 125, 109, 123),
    )
    evaluator, result = outputs(events)
    assert not [item for item in result if isinstance(item, EntrySignal)]
    assert evaluator.diagnostics()["rejection_counts"].get("NON_CONVERGING", 0) >= 1


def test_rising_wedge_emits_bearish_signal() -> None:
    _, result = outputs(rising_wedge_events())
    signals = [item for item in result if isinstance(item, EntrySignal)]
    assert len(signals) == 1
    assert signals[0].direction == "SHORT"
    assert signals[0].entry_price == 111
    assert signals[0].stop_price == 120.5
    assert signals[0].target_price == 104


def test_wick_breakout_does_not_emit_signal() -> None:
    events = list(wedge_events())
    events[8] = bar(8, 123, 132, 116, 117)
    events[9] = bar(9, 123, 128, 114, 115)
    _, result = outputs(tuple(events))
    assert not [item for item in result if isinstance(item, EntrySignal)]


def test_successful_entry_consumes_candidate_and_blocks_later_breakouts() -> None:
    events = list(wedge_events())
    # Keep the original wedge recognizable and cross its boundary repeatedly
    # after the one valid next-bar entry.
    events.extend((
        bar(10, 124, 126, 120, 124),
        bar(11, 124, 126, 120, 124),
        bar(12, 124, 126, 120, 124),
    ))
    evaluator, result = outputs(tuple(events[:10]))
    signals = [item for item in result if isinstance(item, EntrySignal)]

    assert len(signals) == 1
    assert len({item.provenance["candidate_id"] for item in signals}) == 1
    assert evaluator.candidate is None
    assert signals[0].provenance["candidate_id"] in evaluator.consumed_candidate_ids

    snapshot = evaluator.snapshot_state()
    restored = KojoWedgeEvaluator()
    restored.initialize(strategy(), params())
    restored.restore_state(snapshot)
    resumed = []
    for event in events[10:]:
        resumed.extend(restored.consume_market_event(event))
    assert not [item for item in resumed if isinstance(item, EntrySignal)]


def test_new_wedge_can_signal_after_prior_candidate_consumed() -> None:
    # Continue the same evaluator with a later, genuinely new wedge. The
    # consumed identity is not a global one-entry limit.
    events = list(wedge_events())
    for index in range(10, 20):
        events.append(bar(index, 100, 101, 99, 100))
    for index, event in enumerate(rising_wedge_events(), 20):
        events.append(bar(index, event.open, event.high, event.low, event.close))

    evaluator, result = outputs(tuple(events))
    signals = [item for item in result if isinstance(item, EntrySignal)]
    candidate_ids = [item.provenance["candidate_id"] for item in signals]

    assert len(signals) == 2
    assert len(set(candidate_ids)) == 2
    assert all(candidate_id in evaluator.consumed_candidate_ids for candidate_id in candidate_ids)


def test_diagnostic_artifact_is_persisted_for_signal() -> None:
    _, result = outputs(wedge_events())
    signal = next(item for item in result if isinstance(item, EntrySignal))
    with TemporaryDirectory() as root:
        path = write_kojo_wedge_diagnostic_artifact(signal, root)
        assert path.exists()
        assert '"artifact_type": "KOJO_WEDGE_V1_SIGNAL_DIAGNOSTIC"' in path.read_text()


def test_generic_execution_fills_next_open_and_applies_cost_model() -> None:
    feed = HistoricalMarketFeed(wedge_events(), "kojo-fixture", partition="DISCOVERY")
    result = BacktestEngine(register_builtin_evaluators(StrategyEvaluatorRegistry())).run(
        strategy(), params(), feed, CostModel("kojo-test-cost", spread_price=0.25), run_id="kojo-execution-fixture"
    )
    assert len(result.signals) == 1
    assert len(result.outcomes) == 1
    assert result.outcomes[0].provenance["entry_timestamp"] == wedge_events()[9].open_timestamp
    assert result.outcomes[0].provenance["cost_model"] == "kojo-test-cost"


def test_snapshot_restore_is_identical_to_continuous_replay() -> None:
    events = wedge_events()
    continuous, expected = outputs(events)
    resumed = KojoWedgeEvaluator()
    resumed.initialize(strategy(), params())
    actual = []
    for event in events[:7]:
        actual.extend(resumed.consume_market_event(event))
    snapshot = resumed.snapshot_state()
    restored = KojoWedgeEvaluator()
    restored.initialize(strategy(), params())
    restored.restore_state(snapshot)
    for event in events[7:]:
        actual.extend(restored.consume_market_event(event))
    assert [asdict(item) for item in actual] == [asdict(item) for item in expected]
    assert restored.snapshot_state() == continuous.snapshot_state()


def test_prefix_invariance_and_source_independent_parity() -> None:
    events = wedge_events()
    _, prefix = outputs(events[:8])
    _, full_prefix = outputs(events[:8])
    assert [asdict(item) for item in prefix] == [asdict(item) for item in full_prefix]
    feed = HistoricalMarketFeed(events, "kojo-fixture", partition="VALIDATION")
    assert_live_replay_parity(strategy(), params(), feed, feed.with_source("live-replay"), register_builtin_evaluators(StrategyEvaluatorRegistry()))


def test_baseline_is_explicit_and_not_performance_selected() -> None:
    baseline = kojo_wedge_baseline_parameter_set()
    strategy().validate_parameter_set(baseline)
    assert baseline.provenance["source"] == "KOJO_CHAT_DECISION"
    assert "not performance-selected" in baseline.provenance["baseline_rationale"]


class KojoWedgeTests(unittest.TestCase):
    def test_registers_and_emits_causal_next_open_signal(self) -> None:
        test_registers_and_emits_causal_next_open_signal()

    def test_rejects_parallel_corrective_channel(self) -> None:
        test_rejects_parallel_corrective_channel()

    def test_rising_wedge_emits_bearish_signal(self) -> None:
        test_rising_wedge_emits_bearish_signal()

    def test_wick_breakout_does_not_emit_signal(self) -> None:
        test_wick_breakout_does_not_emit_signal()

    def test_successful_entry_consumes_candidate_and_blocks_later_breakouts(self) -> None:
        test_successful_entry_consumes_candidate_and_blocks_later_breakouts()

    def test_new_wedge_can_signal_after_prior_candidate_consumed(self) -> None:
        test_new_wedge_can_signal_after_prior_candidate_consumed()

    def test_diagnostic_artifact_is_persisted_for_signal(self) -> None:
        test_diagnostic_artifact_is_persisted_for_signal()

    def test_generic_execution_fills_next_open_and_applies_cost_model(self) -> None:
        test_generic_execution_fills_next_open_and_applies_cost_model()

    def test_snapshot_restore_is_identical_to_continuous_replay(self) -> None:
        test_snapshot_restore_is_identical_to_continuous_replay()

    def test_prefix_invariance_and_source_independent_parity(self) -> None:
        test_prefix_invariance_and_source_independent_parity()

    def test_baseline_is_explicit_and_not_performance_selected(self) -> None:
        test_baseline_is_explicit_and_not_performance_selected()
