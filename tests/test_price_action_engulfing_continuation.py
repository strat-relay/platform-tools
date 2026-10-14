from __future__ import annotations

import json
import pytest

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_continuation import (
    EngulfingContinuationEvaluator, parameter_set, strategy_version, trend_from_swings,
)
from strategy_backtest.price_action_primitives import Candle, engulfing_confirmation


def bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float, *, tick=0.01) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    return MarketEvent("TEST", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close, provenance={"tick_size": tick, "snapshot_id": "fixture-v1"})


def context() -> tuple[MarketEvent, ...]:
    return (
        bar(0, "H1", 100, 103, 98, 101), bar(1, "H1", 101, 106, 96, 104),
        bar(2, "H1", 104, 110, 99, 107), bar(3, "H1", 107, 108, 90, 98),
        bar(4, "H1", 98, 108, 95, 104), bar(5, "H1", 104, 115, 101, 114),
        bar(6, "H1", 114, 116, 100, 105), bar(7, "H1", 103, 118, 102, 112),
        bar(8, "H1", 112, 130, 105, 125), bar(9, "H1", 125, 125, 100, 110),
        bar(10, "H1", 110, 135, 110, 130), bar(11, "H1", 130, 145, 115, 140),
        bar(12, "H1", 140, 180, 120, 170), bar(13, "H1", 170, 170, 125, 150),
        bar(14, "H1", 150, 175, 130, 160), bar(15, "H1", 160, 165, 135, 155),
    )


def evaluator() -> EngulfingContinuationEvaluator:
    instance = EngulfingContinuationEvaluator()
    instance.initialize(strategy_version(), parameter_set())
    return instance


def test_strict_engulfing_semantics_and_trend_symmetry():
    previous = Candle(0, 0, 1, 110, 111, 104, 105)
    bullish = Candle(1, 1, 2, 105, 133, 104, 132)
    bearish = Candle(1, 1, 2, 132, 133, 104, 105)
    doji = Candle(1, 1, 2, 105, 133, 104, 105)
    assert engulfing_confirmation(previous, bullish, direction="LONG")
    assert not engulfing_confirmation(previous, bearish, direction="LONG")
    assert not engulfing_confirmation(previous, doji, direction="LONG")
    assert trend_from_swings(()) is None


def test_valid_continuation_is_research_only_and_checks_target_availability():
    ev = evaluator()
    outputs = []
    for event in context() + (
                bar(70, "M15", 105, 106, 99.95, 100.0),
                bar(71, "M15", 100.0, 111, 99.9, 110),
    ):
        outputs.extend(ev.consume_market_event(event))
    proposals = [item for item in outputs if item.provenance["rejection_reason"] == "RESEARCH_PROPOSAL_ONLY"]
    assert proposals
    proposal = proposals[0]
    assert proposal.decision.value == "REJECT"
    assert proposal.provenance["target_level"]["known_at"] <= proposal.provenance["information_cutoff"]
    assert proposal.provenance["execution_eligible"] is False
    # The confirmation evaluation cannot contain the next candle's OHLC.
    assert "entry_open" not in proposal.provenance

    checkpoint = json.loads(json.dumps(ev.snapshot_state()))
    restored = evaluator()
    restored.restore_state(checkpoint)
    resumed = restored.consume_market_event(bar(72, "M15", 90, 110, 89, 95))
    assert resumed[0].provenance["rejection_reason"] == "ENTRY_GAP_CANCELLED"


def test_countertrend_and_outside_zone_are_rejected():
    ev = evaluator()
    outputs = [item for event in context() + (
        bar(70, "M15", 140, 141, 140, 140),
        bar(71, "M15", 140, 141, 139, 139),
    ) for item in ev.consume_market_event(event)]
    assert not [item for item in outputs if item.provenance["rejection_reason"] == "RESEARCH_PROPOSAL_ONLY"]


def test_missing_context_same_candle_rule_gap_and_restart():
    ev = evaluator()
    assert any(item.provenance["rejection_reason"] == "INSUFFICIENT_CONTEXT"
               for item in (ev.consume_market_event(bar(0, "M15", 100, 101, 99, 100))))
    ev.consume_market_event(bar(2, "M15", 100, 101, 99, 100))
    with pytest.raises(ValueError):
        ev.consume_market_event(bar(1, "M15", 100, 101, 99, 100))
    # A fresh evaluator demonstrates prefix/restart identity on the context.
    left, right = evaluator(), evaluator()
    expected = [item.canonical_bytes() for event in context() for item in left.consume_market_event(event)]
    for event in context()[:8]:
        right.consume_market_event(event)
    restored = evaluator()
    restored.restore_state(json.loads(json.dumps(right.snapshot_state())))
    actual = [item.canonical_bytes() for event in context()[8:] for item in restored.consume_market_event(event)]
    continuous = evaluator()
    for event in context()[:8]:
        continuous.consume_market_event(event)
    expected_suffix = [item.canonical_bytes() for event in context()[8:] for item in continuous.consume_market_event(event)]
    assert actual == expected_suffix
