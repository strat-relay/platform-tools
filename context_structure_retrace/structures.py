from __future__ import annotations

from itertools import combinations
from typing import Any

from .data import bar_end, iso, timeframe_seconds
from .indicators import atr
from .sr import confirmed_swings


def meaningful_interaction_episodes(distances: list[float], timestamps: list[int], tolerance: float) -> list[dict[str, Any]]:
    """A return counts only after a departure beyond twice the tolerance."""
    episodes: list[dict[str, Any]] = []
    departed = False
    departure_timestamp = None
    for distance, timestamp in zip(distances, timestamps):
        if abs(distance) <= tolerance:
            if departed:
                episodes.append({"touch_timestamp": int(timestamp), "departure_timestamp": departure_timestamp, "type": "RETURN_AFTER_DEPARTURE"})
                departed = False
                departure_timestamp = None
        elif abs(distance) > max(tolerance * 2, 1e-12) and not departed:
            departed = True
            departure_timestamp = int(timestamp)
    return episodes


def _line(a: dict[str, Any], b: dict[str, Any], timestamp: int) -> float:
    dt = max(1, int(b["timestamp"]) - int(a["timestamp"]))
    return float(a["price"]) + (float(b["price"]) - float(a["price"])) * ((int(timestamp) - int(a["timestamp"])) / dt)


def trendline_candidates(bars: list[dict[str, Any]], timeframe: str, as_of: int, lookback: int = 2, max_points: int = 24, *, confirmed_points: list[dict[str, Any]] | None = None, atr_value_override: float | None = None) -> list[dict[str, Any]]:
    points = confirmed_points if confirmed_points is not None else confirmed_swings(bars, timeframe, as_of, lookback)
    points = points[-max_points:]
    available_all = [x for x in bars if bar_end(x, timeframe) <= int(as_of)]
    atr_value = atr_value_override if atr_value_override is not None else (atr(available_all)[-1] if available_all else 0.0)
    tolerance = atr_value * 0.25
    out = []
    for left, right in combinations(points, 2):
        if left["type"] != right["type"] or right["timestamp"] <= left["timestamp"]:
            continue
        touches, violations, penetrations = [], [], []
        available = [x for x in bars if bar_end(x, timeframe) <= int(as_of) and int(x["time"]) >= int(left["timestamp"])]
        distances, timestamps = [], []
        for bar in available:
            line = _line(left, right, int(bar["time"]))
            price = float(bar["low"] if left["type"] == "SUPPORT" else bar["high"])
            distance = price - line if left["type"] == "SUPPORT" else line - price
            distances.append(distance); timestamps.append(int(bar["time"]))
            if abs(distance) <= tolerance:
                touches.append(int(bar["time"]))
            if distance < -tolerance:
                violations.append(int(bar["time"])); penetrations.append(abs(distance))
        meaningful_interactions = meaningful_interaction_episodes(distances, timestamps, tolerance)
        out.append({
            "structure_id": f"TL-{left['type']}-{left['timestamp']}-{right['timestamp']}",
            "schema": "trendline_context", "type": left["type"],
            "anchors": [{"timestamp": iso(left["timestamp"]), "price": left["price"]}, {"timestamp": iso(right["timestamp"]), "price": right["price"]}],
            "slope_price_per_minute": (float(right["price"]) - float(left["price"])) / max(1, (right["timestamp"] - left["timestamp"]) / 60),
            "touches": len(touches), "touch_timestamps": [iso(x) for x in touches],
            "meaningful_interactions": len(meaningful_interactions), "interaction_episodes": meaningful_interactions,
            "violations": len(violations), "violation_timestamps": [iso(x) for x in violations],
            "max_penetration": max(penetrations, default=0.0), "max_penetration_atr": max(penetrations, default=0.0) / atr_value if atr_value else None,
            "tolerance": tolerance, "tolerance_atr": 0.25,
            "slope_atr_per_hour": ((float(right["price"]) - float(left["price"])) / max(1, (right["timestamp"] - left["timestamp"]) / 3600)) / atr_value if atr_value else None,
            "provenance": {"as_of_timestamp": int(as_of), "confirmed_swing_points_only": True, "future_bars_excluded": True, "interaction_rule": "touch -> departure beyond 2x tolerance -> later return"},
        })
    return out


def channel_candidates(bars: list[dict[str, Any]], timeframe: str, as_of: int, lookback: int = 2, max_points: int = 24, *, confirmed_points: list[dict[str, Any]] | None = None, atr_value_override: float | None = None) -> list[dict[str, Any]]:
    points = confirmed_points if confirmed_points is not None else confirmed_swings(bars, timeframe, as_of, lookback)
    points = points[-max_points:]
    highs = [x for x in points if x["type"] == "RESISTANCE"]
    lows = [x for x in points if x["type"] == "SUPPORT"]
    available_all = [x for x in bars if bar_end(x, timeframe) <= int(as_of)]
    atr_value = atr_value_override if atr_value_override is not None else (atr(available_all)[-1] if available_all else 0.0)
    current = float(available_all[-1]["close"]) if available_all else 0.0
    out = []
    for top in combinations(highs, 2):
        for bottom in combinations(lows, 2):
            top_slope = (top[1]["price"] - top[0]["price"]) / max(1, top[1]["timestamp"] - top[0]["timestamp"])
            bottom_slope = (bottom[1]["price"] - bottom[0]["price"]) / max(1, bottom[1]["timestamp"] - bottom[0]["timestamp"])
            if atr_value and abs(top_slope - bottom_slope) * 3600 / atr_value > 0.5:
                continue
            width = ((top[0]["price"] + top[1]["price"]) / 2) - ((bottom[0]["price"] + bottom[1]["price"]) / 2)
            if width <= 0:
                continue
            upper = _line(top[0], top[1], as_of); lower = _line(bottom[0], bottom[1], as_of)
            out.append({
                "structure_id": f"CH-{top[0]['timestamp']}-{top[1]['timestamp']}-{bottom[0]['timestamp']}-{bottom[1]['timestamp']}",
                "schema": "channel_context", "boundaries": {"upper": [top[0], top[1]], "lower": [bottom[0], bottom[1]]},
                "slope_price_per_minute": top_slope * 60, "slope_atr_per_hour": top_slope * 3600 / atr_value if atr_value else None,
                "width": width, "width_atr": width / atr_value if atr_value else None,
                "current_upper": upper, "current_lower": lower,
                "normalized_position": (current - lower) / max(upper - lower, 1e-12),
                "touches_upper": 2, "touches_lower": 2, "violations": [],
                "provenance": {"as_of_timestamp": int(as_of), "confirmed_swing_points_only": True, "future_bars_excluded": True, "candidate_not_selected": True},
            })
    return out
