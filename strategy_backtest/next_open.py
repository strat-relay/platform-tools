"""Opt-in immediate entry adapter for the five frozen PA evaluators.

No strategy source is changed. Reuse each evaluator's entry checks on a copy;
never feed an incomplete candle through setup detection. The caller owns atomic
checkpoint/event persistence and must give this mode a separate run identity.
"""
from copy import deepcopy
from dataclasses import replace

from core.strategies.entry_timing import CandleOpenObservation
from strategy_backtest.models import MarketEvent


def observe_price_action_open(evaluator, observation, *, setup_available_at, now):
    from strategy_backtest.price_action_sr_retest import SupportResistanceRetestEvaluator
    from strategy_backtest.price_action_engulfing_continuation import EngulfingContinuationEvaluator
    from strategy_backtest.price_action_reversal_common import ReversalEvaluator
    if not isinstance(evaluator, (SupportResistanceRetestEvaluator, EngulfingContinuationEvaluator, ReversalEvaluator)):
        raise TypeError('unsupported next-open evaluator; explicit adapter required')
    if not isinstance(observation, CandleOpenObservation):
        raise TypeError('CandleOpenObservation required')
    instrument = observation.instrument
    pending = evaluator.pending.get(instrument)
    if pending is None:
        return ()
    execution = evaluator._execution_events.get(instrument, [])
    if not execution or observation.timeframe_seconds != 900:
        raise ValueError('completed setup and matching execution timeframe required')
    expected = pending.get('expected_next_open_timestamp', execution[-1].close_timestamp)
    if execution[-1].close_timestamp != expected:
        raise ValueError('buffered execution has advanced beyond the pending setup')
    timing = observation.validate(expected_open=expected, setup_available_at=setup_available_at, now=now)
    # Only price/open-time are supplied to _resolve_pending. This event is never
    # appended to candle history and cannot participate in pattern detection.
    event = MarketEvent(instrument, 'M15', observation.open_time, observation.open_time + 900,
                        observation.price, observation.price, observation.price, observation.price,
                        completed=False, source=observation.source,
                        provenance={'snapshot_id': observation.source_event_id})
    working = deepcopy(evaluator)
    evaluation = working._resolve_pending(event)
    if evaluation is None:
        return ()
    provenance = {**evaluation.provenance, 'entry_timing': timing,
                  'entry_classification': timing['entry_classification'],
                  'information_cutoff': observation.open_time,
                  'research_only': True, 'execution_eligible': False,
                  'completed_candle_reconstruction': False,
                  'reconstruction_classification': timing['entry_classification'],
                  'actual_entry_observation_time': observation.observed_at,
                  'entry_candle_data_available_time': observation.available_at}
    evaluation = replace(evaluation, provenance=provenance, decision_time=str(observation.open_time))
    evaluator.pending.pop(instrument)
    evaluator.emitted_evaluations.add(evaluation.evaluation_hash)
    return (evaluation,)
