"""Exact feature_snapshot implementation with shared immutable intermediates."""
from __future__ import annotations

from typing import Any

from context_structure_retrace import SCHEMA_VERSION
from context_structure_retrace.config import DEFAULT_TIMEFRAMES, ResearchTimeframes
from context_structure_retrace.data import CausalReplay, iso
from context_structure_retrace.indicators import atr, ema_context
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.schema import market_snapshot, validate_snapshot
from context_structure_retrace.sr import confirmed_swings, sr_context
from context_structure_retrace.structures import channel_candidates, trendline_candidates
from context_structure_retrace.replay import _direction, _location


def fast_feature_snapshot(
    replay: CausalReplay,
    symbol: str,
    as_of: int,
    quote: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    window_limits: dict[str, int] | None = None,
    timeframes: ResearchTimeframes = DEFAULT_TIMEFRAMES,
    structure_max_points: int = 24,
) -> dict[str, Any]:
    """Return the frozen snapshot, reusing pivots and ATR within one call."""
    working = replay.window(as_of, window_limits)
    contexts: dict[str, dict[str, Any]] = {}
    intermediates: dict[str, tuple[list[dict[str, Any]], float]] = {}
    for timeframe in timeframes.all:
        if timeframe not in working.bars_by_timeframe:
            continue
        view = working.view(timeframe, as_of)
        completed = view["completed_bars"]
        forming = view["forming"]
        points = confirmed_swings(completed, timeframe, as_of, 2)
        atr_value = atr(completed)[-1] if completed else 0.0
        intermediates[timeframe] = (points, atr_value)
        historical_index = working.historical_index(timeframe) if hasattr(working, "historical_index") else None
        contexts[timeframe] = {
            "timeframe": timeframe,
            "completed_direction": _direction(view["completed"]),
            "forming_direction": _direction(forming),
            "completed_candle": view["completed"],
            "forming_candle": forming,
            "completed_location": _location(view["completed"]),
            "forming_location": _location(forming),
            "ema_context": ema_context(completed, forming, as_of, timeframe),
            "sr_context": sr_context(completed, timeframe, as_of, confirmed_points=points, atr_value_override=atr_value, historical_index=historical_index),
            "patterns": detect_patterns(working.bars_by_timeframe.get(timeframe, []), timeframe, as_of, symbol),
            "provenance": view["provenance"],
        }
    execution_completed = working.history(timeframes.execution, as_of)
    structure_timeframe = timeframes.execution
    structure_points, structure_atr = intermediates.get(structure_timeframe, ([], 0.0))
    snapshot = {
        "schema": "context_structure_retrace_feature_snapshot",
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "timestamp": int(as_of), "timestamp_iso": iso(as_of),
        "market_snapshot": market_snapshot(symbol, as_of, quote, contract),
        "timeframes": contexts,
        "trendline_context": trendline_candidates(execution_completed, structure_timeframe, as_of, max_points=structure_max_points, confirmed_points=structure_points, atr_value_override=structure_atr),
        "channel_context": channel_candidates(execution_completed, structure_timeframe, as_of, max_points=structure_max_points, confirmed_points=structure_points, atr_value_override=structure_atr),
        "provenance": {"as_of_timestamp": int(as_of), "source_timeframes": list(timeframes.all), "future_ohlc_exposed": False, "completed_candle_event_rule": True, "structure_timeframe": structure_timeframe, "timeframe_config": {"execution": timeframes.execution, "lower": list(timeframes.lower), "higher": list(timeframes.higher)}, "structure_max_points": structure_max_points, "context_window_limits": window_limits or {"M1": 5000, "M5": 2500, "M15": 1000, "H1": 400, "H4": 100}},
    }
    validate_snapshot(snapshot)
    return snapshot
