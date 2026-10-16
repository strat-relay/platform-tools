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
    provenance = {"tick_size": 0.01, "snapshot_id": "reversal-fixture-v1"}
    if timeframe == "H1":
        # Explicit synthetic event-time watermark: fixture delivery has
        # established H1 coverage through the replay boundary. Only the
        # final fixture event advances beyond the M15 cutoff.
        provenance["context_watermark_timestamp"] = 10_000_000 if index == 10 else (index + 1) * step
    return MarketEvent("TEST", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close,
                       provenance=provenance)


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
        Candle(1, 1, 2, 88.9, 91.2, 88.8, 91.1),
    ), 0.01)["direction"] == "LONG"
    assert engulfing.detect_pattern((
        Candle(0, 0, 1, 109, 111.2, 108.8, 111),
        Candle(1, 1, 2, 111.1, 111.3, 108.3, 108.5),
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
        bar(55, "M15", 88.9, 91.2, 88.8, 91.1),
    ]
    outputs = [item for event in events for item in evaluator.consume_market_event(event)]
    proposal = next(item for item in outputs if item.provenance["rejection_reason"] == "RESEARCH_PROPOSAL_ONLY")
    assert proposal.provenance["execution_eligible"] is False
    assert "entry_open" not in proposal.provenance
    checkpoint = json.loads(json.dumps(evaluator.snapshot_state()))
    restored = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    restored.restore_state(checkpoint)
    original_stop = restored.pending["TEST"]["stop"]
    checkpoint["pending"]["TEST"]["stop"] = -999
    resolved = restored.consume_market_event(bar(56, "M15", 91.1, 92, 90, 91.5))
    assert resolved[0].provenance["rejection_reason"] == "ENTRY_OBSERVED_RESEARCH_ONLY"
    assert resolved[0].provenance["stop"] == original_stop
    assert resolved[0].provenance["accepted_paper_entry_time"] == bar(56, "M15", 91.1, 92, 90, 91.5).open_timestamp


def test_h1_first_and_m15_first_finalize_identical_evaluations():
    h1 = list(h1_context())
    m15 = [
        bar(50, "M15", 96, 96.2, 94.8, 95), bar(51, "M15", 95, 95.2, 92.8, 93),
        bar(52, "M15", 93, 93.2, 90.8, 91), bar(53, "M15", 91, 91.2, 89.8, 90),
        bar(54, "M15", 90, 90.2, 88.8, 89), bar(55, "M15", 88.9, 91.2, 88.8, 91.1),
    ]
    left = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    right = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    left_outputs = [item for event in h1 + m15 for item in left.consume_market_event(event)]
    right_outputs = [item for event in m15 + h1 for item in right.consume_market_event(event)]
    assert [item.canonical_bytes() for item in left_outputs] == [item.canonical_bytes() for item in right_outputs]


def test_completed_candle_reconstruction_does_not_claim_contemporaneous_observation():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    events = list(h1_context()) + [
        bar(50, "M15", 96, 96.2, 94.8, 95), bar(51, "M15", 95, 95.2, 92.8, 93),
        bar(52, "M15", 93, 93.2, 90.8, 91), bar(53, "M15", 91, 91.2, 89.8, 90),
        bar(54, "M15", 90, 90.2, 88.8, 89), bar(55, "M15", 88.9, 91.2, 88.8, 91.1),
    ]
    for event in events:
        evaluator.consume_market_event(event)
    resolved = evaluator.consume_market_event(bar(56, "M15", 91.1, 120, 1, 50))[0]
    assert resolved.provenance["reconstruction_classification"] == "COMPLETED_CANDLE_NEXT_OPEN_RECONSTRUCTION"
    assert resolved.provenance["reference_candle_open_timestamp"] == bar(56, "M15", 91.1, 120, 1, 50).open_timestamp
    assert resolved.provenance["actual_entry_observation_time"] is None
    assert resolved.provenance["observation_timestamp_unknown"] is True
    assert resolved.provenance["entry_open"] == 91.1


def test_invalid_order_duplicate_and_incomplete_context_are_explicit():
    evaluator = initialized(PinBarReversalEvaluator(), pin_version, pin_parameters)
    assert evaluator.consume_market_event(bar(0, "M15", 100, 101, 99, 100)) == ()
    assert evaluator.consume_market_event(bar(0, "M15", 100, 101, 99, 100)) == ()
    with pytest.raises(ValueError, match="out-of-order"):
        evaluator.consume_market_event(bar(-1, "M15", 100, 101, 99, 100))
    assert any(item.provenance["rejection_reason"] == "CONTEXT_UNAVAILABLE_TIMEOUT"
               for item in evaluator.finalize_context("TEST", 1800, timeout=True))


def test_revision_identity_metadata_and_checkpoint_binding():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    original = bar(0, "M15", 100, 101, 99, 100)
    evaluator.consume_market_event(original)
    with pytest.raises(ValueError, match="conflicting candle revision"):
        evaluator.consume_market_event(bar(0, "M15", 100, 102, 99, 100))
    missing_metadata = MarketEvent("TEST", "M15", 900, 1800, 100, 101, 99, 100,
                                   provenance={"tick_size": 0.01})
    with pytest.raises(ValueError, match="snapshot_id"):
        evaluator.consume_market_event(missing_metadata)
    checkpoint = json.loads(json.dumps(evaluator.snapshot_state()))
    checkpoint["binding"]["parameter_hash"] = "wrong"
    restored = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    with pytest.raises(ValueError, match="checkpoint binding"):
        restored.restore_state(checkpoint)


def test_invalid_h1_watermark_is_atomic():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    invalid = MarketEvent("TEST", "H1", 0, 3600, 100, 101, 99, 100,
                          provenance={"tick_size": 0.01, "snapshot_id": "fixture", "context_watermark_timestamp": 3599})
    with pytest.raises(ValueError, match="context_watermark"):
        evaluator.consume_market_event(invalid)
    assert evaluator.snapshot_state()["events"] == []
    assert evaluator.snapshot_state()["candle_content"] == []


def test_strict_engulfing_boundary_and_full_pattern_geometry():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    equal_boundary = evaluator.detect_pattern((
        Candle(0, 0, 1, 91, 91.2, 89, 89),
        Candle(1, 1, 2, 89, 91.2, 88.8, 91),
    ), 0.01)
    assert equal_boundary is None
    accepted = evaluator.detect_pattern((
        Candle(0, 0, 1, 91, 91.2, 89, 89),
        Candle(1, 1, 2, 88.9, 91.5, 88.5, 91.1),
    ), 0.01)
    assert accepted["pattern_high"] == 91.5
    assert accepted["pattern_low"] == 88.5
