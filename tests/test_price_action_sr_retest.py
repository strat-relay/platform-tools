from __future__ import annotations

from dataclasses import asdict

import pytest

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_sr_retest import (
    Decision,
    SupportResistanceRetestEvaluator,
    confirmed_swings,
    parameter_set,
    strategy_version,
)


def bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float, *, instrument="TEST", tick=1.0) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    return MarketEvent(instrument, timeframe, index * step, (index + 1) * step,
                       open_, high, low, close, provenance={"tick_size": tick, "snapshot_id": "fixture-v1"})


def context() -> tuple[MarketEvent, ...]:
    # A confirmed resistance at 110 (center index 2, available at close index 4)
    # and a known opposing support at 90.
    return (
        bar(0, "H1", 100, 103, 98, 101), bar(1, "H1", 101, 106, 95, 104),
        bar(2, "H1", 104, 110, 102, 107), bar(3, "H1", 107, 108, 100, 102),
        bar(4, "H1", 102, 105, 97, 100), bar(5, "H1", 100, 101, 90, 93),
        bar(6, "H1", 93, 96, 89, 91),
        bar(7, "H1", 91, 120, 88, 100), bar(8, "H1", 100, 130, 95, 125),
        bar(9, "H1", 125, 126, 115, 120), bar(10, "H1", 120, 121, 110, 115),
    )


def evaluator() -> SupportResistanceRetestEvaluator:
    instance = SupportResistanceRetestEvaluator()
    instance.initialize(strategy_version(), parameter_set())
    return instance


def test_swing_confirmation_lag_and_prefix_invariance():
    candles = tuple(__import__("strategy_backtest.price_action_sr_retest", fromlist=["Candle"]).Candle.from_event(i, event) for i, event in enumerate(context()))
    assert not [s for s in confirmed_swings(candles[:4], left=2, right=2) if s.bar_index == 2]
    swings = confirmed_swings(candles, left=2, right=2)
    resistance = next(s for s in swings if s.kind == "RESISTANCE")
    assert resistance.bar_index == 2
    assert resistance.known_at == candles[4].close_timestamp


def test_invalid_and_out_of_order_data_fail_closed():
    ev = evaluator()
    with pytest.raises(ValueError):
        ev.consume_market_event(bar(1, "H1", 1, 0, 2, 1))
    ev.consume_market_event(context()[0])
    ev.consume_market_event(context()[2])
    with pytest.raises(ValueError):
        ev.consume_market_event(context()[1])
    # Duplicate events are idempotent, including when repeated at the prefix.
    fresh = evaluator()
    assert fresh.consume_market_event(context()[0]) == ()
    assert fresh.consume_market_event(context()[0]) == ()


def test_research_evaluator_emits_no_executable_signal_and_records_provenance():
    ev = evaluator()
    outputs = []
    for event in context():
        outputs.extend(ev.consume_market_event(event))
    # Add M15 bars after the confirmed H1 structure: break, non-confirming touch,
    # then a bullish rejection. The trace is still research-only.
    m15 = (
        bar(50, "M15", 100, 109, 99, 105, tick=0.01),
        bar(51, "M15", 105, 115, 104, 114, tick=0.01),
        bar(52, "M15", 114, 115, 109, 110, tick=0.01),
        bar(53, "M15", 110, 116, 108, 116, tick=0.01),
    )
    for event in m15:
        outputs.extend(ev.consume_market_event(event))
    assert outputs
    assert all(item.provenance["research_only"] is True for item in outputs)
    assert all(item.provenance["execution_eligible"] is False for item in outputs)
    assert all(item.decision != Decision.SIGNAL for item in outputs)
    assert all("parameter_hash" in item.provenance for item in outputs)
    proposals = [item for item in outputs if item.provenance["rejection_reason"] == "RESEARCH_PROPOSAL_ONLY"]
    assert proposals
    assert proposals[0].provenance["target_level"]["known_at"] <= proposals[0].provenance["information_cutoff"]


def test_restart_equivalence_and_repeated_evaluation():
    events = context()
    left = evaluator()
    expected = []
    for event in events:
        expected.extend(left.consume_market_event(event))
    right = evaluator()
    actual = []
    for event in events[:3]:
        actual.extend(right.consume_market_event(event))
    snapshot = right.snapshot_state()
    restored = evaluator()
    restored.restore_state(snapshot)
    for event in events[3:]:
        actual.extend(restored.consume_market_event(event))
    assert [item.canonical_bytes() for item in actual] == [item.canonical_bytes() for item in expected]
    assert restored.consume_market_event(events[-1]) == ()


def test_json_checkpoint_preserves_pending_next_open_decision():
    import json

    ev = evaluator()
    for event in context():
        ev.consume_market_event(event)
    for event in (
        bar(50, "M15", 100, 109, 99, 105, tick=0.01),
        bar(51, "M15", 105, 115, 104, 114, tick=0.01),
        bar(52, "M15", 114, 115, 109, 110, tick=0.01),
        bar(53, "M15", 110, 116, 108, 116, tick=0.01),
    ):
        ev.consume_market_event(event)
    checkpoint = json.loads(json.dumps(ev.snapshot_state()))
    restored = evaluator()
    restored.restore_state(checkpoint)
    result = restored.consume_market_event(bar(54, "M15", 100, 110, 90, 95, tick=0.01))
    assert result[0].provenance["rejection_reason"] == "ENTRY_GAP_CANCELLED"


def test_long_short_symmetry_is_reflected_in_contract():
    assert strategy_version().strategy_version_id.endswith("@V1")
    assert parameter_set().values["execution_timeframe"] == "M15"
    assert parameter_set().values["context_timeframe"] == "H1"
