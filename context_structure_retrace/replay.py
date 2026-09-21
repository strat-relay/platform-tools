from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION
from .data import CausalReplay, bar_end, iso
from .indicators import ema_context
from .patterns import detect_patterns
from .schema import market_snapshot, validate_snapshot
from .sr import sr_context
from .structures import channel_candidates, trendline_candidates
from .config import DEFAULT_TIMEFRAMES, ResearchTimeframes


def _direction(bar: dict[str, Any] | None) -> str:
    if not bar:
        return "UNKNOWN"
    o, c = float(bar["open"]), float(bar["close"])
    return "UP" if c > o else "DOWN" if c < o else "DOJI"


def _location(bar: dict[str, Any] | None) -> dict[str, Any]:
    if not bar:
        return {"range": None, "location": None, "movement": None}
    high, low, close, open_ = [float(bar[k]) for k in ("high", "low", "close", "open")]
    rng = max(high - low, 1e-12)
    return {"range": rng, "location": (close - low) / rng, "movement": close - open_}


def timeframe_context(replay: CausalReplay, timeframe: str, as_of: int, symbol: str) -> dict[str, Any]:
    view = replay.view(timeframe, as_of)
    completed = view["completed_bars"]
    forming = view["forming"]
    return {
        "timeframe": timeframe,
        "completed_direction": _direction(view["completed"]),
        "forming_direction": _direction(forming),
        "completed_candle": view["completed"],
        "forming_candle": forming,
        "completed_location": _location(view["completed"]),
        "forming_location": _location(forming),
        "ema_context": ema_context(completed, forming, as_of, timeframe),
        "sr_context": sr_context(completed, timeframe, as_of),
        "patterns": detect_patterns(replay.bars_by_timeframe.get(timeframe, []), timeframe, as_of, symbol),
        "provenance": view["provenance"],
    }


def feature_snapshot(replay: CausalReplay, symbol: str, as_of: int, quote: dict[str, Any] | None = None, contract: dict[str, Any] | None = None, window_limits: dict[str, int] | None = None, timeframes: ResearchTimeframes = DEFAULT_TIMEFRAMES, structure_max_points: int = 24) -> dict[str, Any]:
    working = replay.window(as_of, window_limits)
    context = {tf: timeframe_context(working, tf, as_of, symbol) for tf in timeframes.all if tf in working.bars_by_timeframe}
    execution_completed = working.history(timeframes.execution, as_of)
    structure_timeframe = timeframes.execution
    structure_completed = working.history(structure_timeframe, as_of)
    snapshot = {
        "schema": "context_structure_retrace_feature_snapshot",
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "timestamp": int(as_of), "timestamp_iso": iso(as_of),
        "market_snapshot": market_snapshot(symbol, as_of, quote, contract),
        "timeframes": context,
        "trendline_context": trendline_candidates(structure_completed, structure_timeframe, as_of, max_points=structure_max_points),
        "channel_context": channel_candidates(structure_completed, structure_timeframe, as_of, max_points=structure_max_points),
        "provenance": {"as_of_timestamp": int(as_of), "source_timeframes": list(timeframes.all), "future_ohlc_exposed": False, "completed_candle_event_rule": True, "structure_timeframe": structure_timeframe, "timeframe_config": {"execution": timeframes.execution, "lower": list(timeframes.lower), "higher": list(timeframes.higher)}, "structure_max_points": structure_max_points, "context_window_limits": window_limits or {"M1": 5000, "M5": 2500, "M15": 1000, "H1": 400, "H4": 100}},
    }
    validate_snapshot(snapshot)
    return snapshot


def replay_pattern_events(replay: CausalReplay, symbol: str, timeframe: str | None = None, timeframes: ResearchTimeframes = DEFAULT_TIMEFRAMES) -> Iterator[dict[str, Any]]:
    timeframe = timeframe or timeframes.execution
    bars = replay.bars_by_timeframe.get(timeframe, [])
    for bar in bars:
        as_of = bar_end(bar, timeframe)
        events = detect_patterns(bars, timeframe, as_of, symbol)
        for event in events:
            snapshot = feature_snapshot(replay, symbol, as_of, timeframes=timeframes)
            yield {"event": event, "context_snapshot": snapshot, "event_id": event["event_id"]}


def write_jsonl(rows: Iterator[dict[str, Any]], path: str | Path) -> int:
    count = 0
    with Path(path).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            count += 1
    return count
