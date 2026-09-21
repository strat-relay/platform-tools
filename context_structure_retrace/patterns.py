from __future__ import annotations

import hashlib
from typing import Any

from .data import bar_end
from .indicators import atr


def _event_id(symbol: str, timeframe: str, timestamp: int, pattern: str) -> str:
    return hashlib.sha256(f"{symbol}|{timeframe}|{timestamp}|{pattern}".encode()).hexdigest()[:20]


def detect_patterns(bars: list[dict[str, Any]], timeframe: str, as_of: int, symbol: str, completed_override: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    completed = completed_override if completed_override is not None else [x for x in bars if bar_end(x, timeframe) <= int(as_of)]
    if not completed:
        return []
    i = len(completed) - 1
    if i < 2:
        return []
    c0, c1, c2 = completed[i - 2], completed[i - 1], completed[i]
    o0, h0, l0, cl0 = [float(c0[k]) for k in ("open", "high", "low", "close")]
    o1, h1, l1, cl1 = [float(c1[k]) for k in ("open", "high", "low", "close")]
    o2, h2, l2, cl2 = [float(c2[k]) for k in ("open", "high", "low", "close")]
    # ``atr`` only uses the trailing period for its final value. Restricting
    # the input to the trailing window preserves the value while keeping
    # historical replay linear rather than repeatedly rescanning all history.
    atr_value = atr(completed[-15:-1])[-1] if len(completed) > 1 else 0.0
    body0, body1, body2 = abs(cl0 - o0), abs(cl1 - o1), abs(cl2 - o2)
    rng2 = max(h2 - l2, 1e-12)
    candidates: list[tuple[str, dict[str, Any]]] = []
    if cl1 < o1 and cl2 > o2 and o2 <= cl1 and cl2 >= o1:
        candidates.append(("BULLISH_ENGULFING", {"previous_body": body1, "current_body": body2}))
    if cl1 > o1 and cl2 < o2 and o2 >= cl1 and cl2 <= o1:
        candidates.append(("BEARISH_ENGULFING", {"previous_body": body1, "current_body": body2}))
    if cl0 < o0 and body1 <= min(atr_value * 0.50, body0 * 0.50) and cl2 > o2 and body2 >= atr_value * 0.75 and cl2 >= (o0 + cl0) / 2:
        candidates.append(("MORNING_STAR", {"first_body": body0, "middle_body": body1, "third_body": body2}))
    if cl0 > o0 and body1 <= min(atr_value * 0.50, body0 * 0.50) and cl2 < o2 and body2 >= atr_value * 0.75 and cl2 <= (o0 + cl0) / 2:
        candidates.append(("EVENING_STAR", {"first_body": body0, "middle_body": body1, "third_body": body2}))
    upper, lower = h2 - max(o2, cl2), min(o2, cl2) - l2
    close_location = (cl2 - l2) / rng2
    if lower >= max(body2 * 2, 1e-12) and close_location >= 0.65:
        candidates.append(("BULLISH_REJECTION_WICK", {"lower_wick": lower, "body": body2}))
    if upper >= max(body2 * 2, 1e-12) and close_location <= 0.35:
        candidates.append(("BEARISH_REJECTION_WICK", {"upper_wick": upper, "body": body2}))
    out = []
    for pattern, measurements in candidates:
        out.append({
            "event_id": _event_id(symbol, timeframe, int(c2["time"]), pattern),
            "schema": "pattern_context", "symbol": symbol, "timeframe": timeframe,
            "timestamp": int(c2["time"]) + (bar_end(c2, timeframe) - int(c2["time"])),
            "pattern": pattern, "direction": "LONG" if pattern.startswith("BULL") or pattern == "MORNING_STAR" else "SHORT",
            "measurements": {**measurements, "range": h2 - l2, "body": body2, "body_atr": body2 / atr_value if atr_value else None, "close_location": close_location, "upper_wick": upper, "lower_wick": lower},
            "source_bar_timestamps": [int(c0["time"]), int(c1["time"]), int(c2["time"])],
            "provenance": {"as_of_timestamp": int(as_of), "completed_bar_only": True, "future_bars_excluded": True, "thresholds_are_descriptive": True},
        })
    return out
