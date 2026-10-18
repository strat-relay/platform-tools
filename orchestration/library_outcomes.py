"""Canonical resolver dispatch for the frozen library research outcome policy."""
from dataclasses import asdict
from datetime import datetime, timezone

from outcome_resolver import OutcomeResolution
from strategy_backtest.research_outcomes import ResearchOutcomeEntry, resolve_research_entry


def resolve_library_signal(signal, candles, timeframe_minutes):
    entry = ResearchOutcomeEntry(
        signal.signal_id, signal.strategy_id+'@'+str(signal.strategy_metadata.get('strategy_version','V1')), signal.instrument,
        signal.direction, int(signal.decision_time.timestamp()), signal.entry,
        signal.stop, signal.target,
        parameter_hash=signal.strategy_metadata.get('parameter_hash'))
    if any(c.open is None for c in candles):
        return OutcomeResolution('OPEN', None, None, 'INSUFFICIENT_DATA',
                                 'LIBRARY_SHARED_RESEARCH_POLICY_V1',
                                 {'reason':'MISSING_CANDLE_OPEN', 'unprocessed_path':True})
    bars = [{'time': int(c.open_timestamp.timestamp()), 'open': c.open,
             'high': c.high, 'low': c.low, 'close': c.close} for c in candles]
    outcome = resolve_research_entry(entry, bars, timeframe_seconds=timeframe_minutes*60)
    status = {'TP': 'TARGET_HIT', 'SL': 'STOPPED'}.get(outcome.status, 'OPEN')
    state = ('RESOLVED' if status != 'OPEN' else
             'AMBIGUOUS_INTRABAR' if outcome.status == 'AMBIGUOUS_INTRABAR' else
             'CANDLE_GAP' if outcome.status == 'DATA_GAP_UNRESOLVED' else 'INSUFFICIENT_DATA')
    evidence = asdict(outcome)
    if state == 'CANDLE_GAP':
        evidence['unprocessed_path'] = True
    return OutcomeResolution(
        status, outcome.realized_r,
        datetime.fromtimestamp(outcome.exit_timestamp, timezone.utc) if outcome.exit_timestamp is not None else None,
        state, 'LIBRARY_SHARED_RESEARCH_POLICY_V1', evidence,
        exit_price=outcome.exit_price, activation_price=signal.entry)
