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
        provenance["context_watermark_timestamp"] = (index + 1) * step
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
        (102, 105, 97, 102), (102, 105, 97, 102), (102, 105, 97, 102),
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

CONTRACTS = [
    (EngulfingReversalEvaluator, engulfing_version, engulfing_parameters),
    (StarReversalEvaluator, star_version, star_parameters),
    (PinBarReversalEvaluator, pin_version, pin_parameters),
]


def lifecycle_events(cls, short=False):
    prior = [(96, 96.2, 94.8, 95), (95, 95.2, 92.8, 93),
             (93, 93.2, 90.8, 91)]
    patterns = {
        EngulfingReversalEvaluator: [(91, 91.2, 89.8, 90), (90, 90.2, 88.8, 89), (88.9, 91.2, 88.8, 91.1)],
        StarReversalEvaluator: [(91, 91.2, 88.8, 89.5), (89.5, 90, 89.2, 89.7), (89.5, 91.5, 88.8, 91.2)],
        PinBarReversalEvaluator: [(91, 91.2, 89.8, 90), (90, 90.2, 88.8, 89), (89.1, 89.35, 88.6, 89.3)],
    }
    values = prior + patterns[cls]
    entry = values[-1][-1]
    values.append((entry, entry + 1, entry - .5, entry + .2))
    events = list(h1_context()) + [bar(50+i, 'M15', *v) for i, v in enumerate(values)]
    if short:
        from dataclasses import replace
        events = [replace(e, open=200-e.open, high=200-e.low, low=200-e.high, close=200-e.close) for e in events]
    return events


@pytest.mark.parametrize('cls,version,parameters', CONTRACTS)
@pytest.mark.parametrize('short', [False, True])
def test_full_lifecycle_matrix(cls, version, parameters, short):
    evaluator = initialized(cls(), version, parameters)
    outputs = [o for e in lifecycle_events(cls, short) for o in evaluator.consume_market_event(e)]
    proposals = [o for o in outputs if o.provenance['rejection_reason'] == 'RESEARCH_PROPOSAL_ONLY']
    entries = [o for o in outputs if o.provenance['rejection_reason'] == 'ENTRY_OBSERVED_RESEARCH_ONLY']
    assert len(proposals) == len(entries) == 1
    assert entries[0].direction == ('SHORT' if short else 'LONG')
    assert entries[0].candidate_id == proposals[0].candidate_id
    assert entries[0].provenance['completed_candle_reconstruction'] is True
    assert not evaluator.pending


@pytest.mark.parametrize('cls,version,parameters', CONTRACTS)
def test_timeout_without_buffer_terminates_once_and_survives_restart(cls, version, parameters):
    evaluator = initialized(cls(), version, parameters)
    for event in lifecycle_events(cls)[:-1]:
        evaluator.consume_market_event(event)
    candidate = evaluator.pending['TEST']['candidate_id']
    watermark = evaluator.context_watermarks['TEST']
    cancelled = evaluator.finalize_context('TEST', 57*900, timeout=True)
    assert len(cancelled) == 1
    assert cancelled[0].candidate_id == candidate
    assert cancelled[0].provenance['entry_cancelled']
    assert evaluator.context_watermarks['TEST'] == watermark
    assert evaluator.finalize_context('TEST', 57*900, timeout=True) == ()
    restored = initialized(cls(), version, parameters)
    restored.restore_state(json.loads(json.dumps(evaluator.snapshot_state())))
    outputs = restored.consume_market_event(lifecycle_events(cls)[-1])
    assert not any(o.provenance['rejection_reason'] == 'ENTRY_OBSERVED_RESEARCH_ONLY' for o in outputs)
    assert not restored.pending


@pytest.mark.parametrize('tick', [0, -1, float('nan'), float('inf')])
def test_invalid_tick_warmup_is_atomic(tick):
    from dataclasses import replace
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    before = evaluator.snapshot_state()
    event = replace(bar(0, 'M15', 100, 101, 99, 100), provenance={'tick_size': tick, 'snapshot_id': 'test'})
    with pytest.raises(ValueError, match='tick_size'):
        evaluator.consume_market_event(event)
    assert evaluator.snapshot_state() == before


def test_real_boundary_delayed_h1_and_buffered_restart():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    for event in h1_context()[:11]:
        evaluator.consume_market_event(event)
    # 11:15 uses the completed 11:00 H1; 12:00 needs H1 closing at 12:00.
    assert evaluator.consume_market_event(bar(44, 'M15', 100, 101, 99, 100))
    assert evaluator.consume_market_event(bar(47, 'M15', 100, 101, 99, 100)) == ()
    restored = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    restored.restore_state(json.loads(json.dumps(evaluator.snapshot_state())))
    released = restored.consume_market_event(h1_context()[11])
    assert len(released) == 1
    with pytest.raises(ValueError, match='coverage'):
        restored.finalize_context('TEST', 14*3600)

@pytest.mark.parametrize('cls,version,parameters', CONTRACTS)
def test_entry_ohlc_independence_missing_candle_and_gap(cls, version, parameters):
    from dataclasses import replace
    events = lifecycle_events(cls)
    def run(entry):
        evaluator = initialized(cls(), version, parameters)
        for event in events[:-1]:
            evaluator.consume_market_event(event)
        return evaluator, evaluator.consume_market_event(entry)
    original = events[-1]
    _, a = run(original)
    _, b = run(replace(original, high=150, low=1, close=50))
    reason = 'ENTRY_OBSERVED_RESEARCH_ONLY'
    first = next(o for o in a if o.provenance['rejection_reason'] == reason)
    second = next(o for o in b if o.provenance['rejection_reason'] == reason)
    for key in ['entry_open', 'stop', 'target', 'actual_entry_r', 'accepted_paper_entry_time']:
        assert first.provenance[key] == second.provenance[key]
    _, missing = run(replace(original, open_timestamp=original.open_timestamp+900,
                             close_timestamp=original.close_timestamp+900))
    assert missing[0].provenance['rejection_reason'] == 'MISSING_EXPECTED_NEXT_CANDLE'
    assert missing[0].provenance['entry_open'] is None
    _, gap = run(replace(original, open=original.open-.1,
                         low=min(original.low, original.open-.1)))
    assert gap[0].provenance['rejection_reason'] == 'ENTRY_GAP_CANCELLED'


@pytest.mark.parametrize('cls,version,parameters', CONTRACTS)
def test_order_prefix_and_restart_matrix(cls, version, parameters):
    events = lifecycle_events(cls)
    context = [e for e in events if e.timeframe == 'H1']
    execution = [e for e in events if e.timeframe == 'M15']
    left = initialized(cls(), version, parameters)
    right = initialized(cls(), version, parameters)
    a = [o.canonical_bytes() for e in context+execution for o in left.consume_market_event(e)]
    b = [o.canonical_bytes() for e in execution for o in right.consume_market_event(e)]
    restored = initialized(cls(), version, parameters)
    restored.restore_state(json.loads(json.dumps(right.snapshot_state())))
    b += [o.canonical_bytes() for e in context for o in restored.consume_market_event(e)]
    assert a == b
    prefix = initialized(cls(), version, parameters)
    c = [o.canonical_bytes() for e in events[:-1] for o in prefix.consume_market_event(e)]
    assert a[:len(c)] == c


def test_missing_h1_does_not_assert_healthy_coverage():
    evaluator = initialized(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters)
    for event in h1_context()[:11]:
        evaluator.consume_market_event(event)
    # Skip H1 closing at 12:00, then supply 13:00. Missing coverage stays blocked.
    evaluator.consume_market_event(h1_context()[12])
    assert evaluator.consume_market_event(bar(51, 'M15', 100, 101, 99, 100)) == ()
    outputs = evaluator.finalize_context('TEST', 13*3600, timeout=True)
    assert outputs[0].provenance['rejection_reason'] == 'CONTEXT_UNAVAILABLE_TIMEOUT'
