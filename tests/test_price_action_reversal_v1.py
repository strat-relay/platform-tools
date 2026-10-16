from __future__ import annotations

import json

import pytest

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_reversal import (
    EngulfingReversalEvaluator, parameter_set as engulfing_parameters,
    strategy_version as engulfing_version,
)
from strategy_backtest.price_action_pin_bar_reversal import (
    PinBarReversalEvaluator, parameter_set as pin_parameters,
    strategy_version as pin_version,
)
from strategy_backtest.price_action_primitives import Candle
from strategy_backtest.price_action_star_reversal import (
    StarReversalEvaluator, parameter_set as star_parameters,
    strategy_version as star_version,
)


def bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    return MarketEvent("TEST", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close,
                       provenance={"tick_size": 0.01, "snapshot_id": "reversal-fixture-v1"})


def h1_context() -> tuple[MarketEvent, ...]:
    # The low at 89 and high at 110 are confirmed only after two later H1 bars.
    return tuple(bar(i, "H1", *values) for i, values in enumerate((
        (100, 103, 98, 101), (101, 105, 96, 103), (103, 108, 94, 106),
        (106, 108, 92, 98), (98, 104, 95, 101), (101, 110, 93, 104),
        (104, 104, 89, 96), (96, 108, 94, 103), (103, 107, 95, 101),
        (101, 110, 96, 105), (105, 106, 98, 102),
    )))


def initialized(evaluator, version, parameters):
    evaluator.initialize(version(), parameters())
    return evaluator


def test_three_contracts_are_distinct_research_only_and_execution_ineligible():
    contracts = (
        (EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters),
        (StarReversalEvaluator(), star_version, star_parameters),
        (PinBarReversalEvaluator(), pin_version, pin_parameters),
    )
    ids = set()
    for evaluator, version, parameters in contracts:
        initialized(evaluator, version, parameters)
        ids.add(evaluator.strategy_version_id)
        assert evaluator.strategy_version.lifecycle == "RESEARCH"
        assert evaluator.diagnostics()["research_only"] is True
    assert len(ids) == 3


def test_pattern_detectors_cover_long_short_and_zero_body_boundaries():
    engulfing = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    assert engulfing.detect_pattern((
        Candle(0, 0, 1, 91, 91.2, 88.8, 89),
        Candle(1, 1, 2, 88.9, 91.2, 88.8, 91),
    ), 0.01)["direction"] == "LONG"
    assert engulfing.detect_pattern((
        Candle(0, 0, 1, 109, 111.2, 108.8, 111),
        Candle(1, 1, 2, 111, 111.2, 108.3, 108.5),
    ), 0.01)["direction"] == "SHORT"

    star = initialized(StarReversalEvaluator(), star_version, star_parameters)
    assert star.detect_pattern((
        Candle(0, 0, 1, 91, 91.2, 88.9, 89.5),
        Candle(1, 1, 2, 89.5, 90, 89.2, 89.7),
        Candle(2, 2, 3, 89.5, 91.5, 88.8, 91.2),
    ), 0.01)["direction"] == "LONG"

    pin = initialized(PinBarReversalEvaluator(), pin_version, pin_parameters)
    zero_body = pin.detect_pattern((Candle(0, 0, 1, 89, 89, 88, 89),), 0.01)
    zero_range = pin.detect_pattern((Candle(0, 0, 1, 89, 89, 89, 89),), 0.01)
    assert zero_body["rejection_reason"] == "ZERO_BODY_UNSUPPORTED"
    assert zero_range["rejection_reason"] == "ZERO_RANGE_INVALID"


def test_causal_replay_proposes_then_resolves_next_open_and_survives_restart():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    events = list(h1_context()) + [
        bar(50, "M15", 96, 96.2, 94.8, 95),
        bar(51, "M15", 95, 95.2, 92.8, 93),
        bar(52, "M15", 93, 93.2, 90.8, 91),
        bar(53, "M15", 91, 91.2, 89.8, 90),
        bar(54, "M15", 90, 90.2, 88.8, 89),
        bar(55, "M15", 89, 91.2, 88.8, 91),
    ]
    outputs = [item for event in events for item in evaluator.consume_market_event(event)]
    proposal = next(item for item in outputs if item.provenance["rejection_reason"] == "RESEARCH_PROPOSAL_ONLY")
    assert proposal.provenance["execution_eligible"] is False
    assert "entry_open" not in proposal.provenance
    checkpoint = json.loads(json.dumps(evaluator.snapshot_state()))
    restored = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    restored.restore_state(checkpoint)
    resolved = restored.consume_market_event(bar(55, "M15", 91, 92, 90, 91.5))
    assert resolved[0].provenance["rejection_reason"] == "ENTRY_OBSERVED_RESEARCH_ONLY"
    assert resolved[0].provenance["accepted_paper_entry_time"] == bar(55, "M15", 91, 92, 90, 91.5).open_timestamp


def test_invalid_order_duplicate_and_incomplete_context_are_explicit():
    evaluator = initialized(PinBarReversalEvaluator(), pin_version, pin_parameters)
    assert evaluator.consume_market_event(bar(0, "M15", 100, 101, 99, 100))
    assert evaluator.consume_market_event(bar(0, "M15", 100, 101, 99, 100)) == ()
    with pytest.raises(ValueError, match="out-of-order"):
        evaluator.consume_market_event(bar(-1, "M15", 100, 101, 99, 100))
    assert any(item.provenance["rejection_reason"] == "INSUFFICIENT_CONTEXT"
               for item in evaluator.consume_market_event(bar(1, "M15", 100, 101, 99, 100)))
