from __future__ import annotations

from collections import defaultdict
from typing import Any

from .data import bar_end, iso, timeframe_seconds
from .indicators import atr


def confirmed_swings(bars: list[dict[str, Any]], timeframe: str, as_of: int, lookback: int = 2) -> list[dict[str, Any]]:
    available = [x for x in bars if bar_end(x, timeframe) <= int(as_of)]
    out: list[dict[str, Any]] = []
    for i in range(lookback, len(available) - lookback):
        future_confirmation = bar_end(available[i + lookback], timeframe)
        if future_confirmation > int(as_of):
            continue
        hi = float(available[i]["high"]); lo = float(available[i]["low"])
        if hi >= max(float(x["high"]) for x in available[i - lookback:i + lookback + 1]):
            out.append({"type": "RESISTANCE", "price": hi, "timestamp": int(available[i]["time"]), "confirmed_at": future_confirmation})
        if lo <= min(float(x["low"]) for x in available[i - lookback:i + lookback + 1]):
            out.append({"type": "SUPPORT", "price": lo, "timestamp": int(available[i]["time"]), "confirmed_at": future_confirmation})
    return out


def _touch_reaction(bar: dict[str, Any], center: float, zone_low: float, zone_high: float, atr_value: float, kind: str) -> dict[str, Any] | None:
    high, low = float(bar["high"]), float(bar["low"])
    if high < zone_low or low > zone_high:
        return None
    open_, close = float(bar["open"]), float(bar["close"])
    body_low, body_high = min(open_, close), max(open_, close)
    penetration = max(0.0, zone_high - low) if kind == "SUPPORT" else max(0.0, high - zone_low)
    body_penetration = max(0.0, zone_high - body_low) if kind == "SUPPORT" else max(0.0, body_high - zone_low)
    rejection = max(0.0, close - center) if kind == "SUPPORT" else max(0.0, center - close)
    return {
        "timestamp": int(bar["time"]),
        "reaction_magnitude": abs(close - center),
        "reaction_magnitude_atr": abs(close - center) / atr_value if atr_value else None,
        "wick_penetration": penetration,
        "wick_penetration_atr": penetration / atr_value if atr_value else None,
        "body_penetration": body_penetration,
        "body_penetration_atr": body_penetration / atr_value if atr_value else None,
        "rejection_distance": rejection,
        "atr_normalized_rejection": rejection / atr_value if atr_value else None,
        "direction_after_touch": "UP" if close > open_ else "DOWN" if close < open_ else "FLAT",
    }


def build_zones(bars: list[dict[str, Any]], timeframe: str, as_of: int, lookback: int = 2, cluster_tolerance_atr: float = 0.25, *, confirmed_points: list[dict[str, Any]] | None = None, atr_value_override: float | None = None, historical_index: Any | None = None) -> list[dict[str, Any]]:
    available = [x for x in bars if bar_end(x, timeframe) <= int(as_of)]
    if len(available) < lookback * 2 + 5:
        return []
    points = confirmed_points if confirmed_points is not None else confirmed_swings(available, timeframe, as_of, lookback)
    atr_value = atr_value_override if atr_value_override is not None else (atr(available)[-1] if atr(available) else 0.0)
    tolerance = max(atr_value * cluster_tolerance_atr, 1e-12)
    zones: list[dict[str, Any]] = []
    for point in sorted(points, key=lambda x: (x["type"], x["price"], x["timestamp"])):
        match = next((z for z in zones if z["type"] == point["type"] and abs(point["price"] - z["midpoint"]) <= tolerance), None)
        if match is None:
            zones.append({"type": point["type"], "prices": [point["price"]], "reaction_anchors": [point], "midpoint": point["price"]})
        else:
            match["prices"].append(point["price"]); match["reaction_anchors"].append(point); match["midpoint"] = sum(match["prices"]) / len(match["prices"])

    close = float(available[-1]["close"])
    output = []
    for index, zone in enumerate(zones, 1):
        midpoint = float(zone["midpoint"]); zone_low = min(zone["prices"]) - tolerance; zone_high = max(zone["prices"]) + tolerance
        first_seen = min(int(x["timestamp"]) for x in zone["reaction_anchors"])
        reactions = []
        reaction_bars = historical_index.intersecting(zone_low, zone_high, as_of, start_open=max(first_seen, int(available[0]["time"]))) if historical_index is not None else available
        for bar in reaction_bars:
            reaction = _touch_reaction(bar, midpoint, zone_low, zone_high, atr_value, zone["type"])
            if reaction:
                reactions.append(reaction)
        role = zone["type"]
        flips = []
        role_bars = historical_index.outside_close(zone_low, zone_high, as_of, start_open=int(available[0]["time"])) if historical_index is not None else available
        for bar in role_bars:
            c = float(bar["close"])
            if zone["type"] == "SUPPORT" and c < zone_low:
                if role != "RESISTANCE": flips.append({"timestamp": int(bar["time"]), "from": role, "to": "RESISTANCE"})
                role = "RESISTANCE"
            elif zone["type"] == "RESISTANCE" and c > zone_high:
                if role != "SUPPORT": flips.append({"timestamp": int(bar["time"]), "from": role, "to": "SUPPORT"})
                role = "SUPPORT"
        output.append({
            "level_id": f"{zone['type']}-{first_seen}-{index}",
            "type": zone["type"], "support_resistance_role": role,
            "zone_low": zone_low, "zone_high": zone_high, "midpoint": midpoint,
            "first_seen": iso(first_seen), "last_reaction": iso(max((x["timestamp"] for x in reactions), default=first_seen)),
            "reaction_count": len(reactions), "reaction_timestamps": [iso(x["timestamp"]) for x in reactions],
            "reactions": reactions, "zone_width": zone_high - zone_low,
            "zone_width_atr": (zone_high - zone_low) / atr_value if atr_value else None,
            "distance_from_current_price": midpoint - close,
            "distance_from_current_price_atr": (midpoint - close) / atr_value if atr_value else None,
            "age_seconds": int(as_of) - first_seen,
            "recency_seconds": int(as_of) - max((x["timestamp"] for x in reactions), default=first_seen),
            "role_flips": flips,
            "cluster_tolerance": tolerance, "cluster_tolerance_atr": cluster_tolerance_atr,
            "provenance": {"as_of_timestamp": int(as_of), "timeframe": timeframe, "swing_lookback": lookback, "confirmed_points_only": True, "future_bars_excluded": True},
        })
    return output


def sr_context(bars: list[dict[str, Any]], timeframe: str, as_of: int, *, confirmed_points: list[dict[str, Any]] | None = None, atr_value_override: float | None = None, historical_index: Any | None = None) -> dict[str, Any]:
    zones = build_zones(bars, timeframe, as_of, confirmed_points=confirmed_points, atr_value_override=atr_value_override, historical_index=historical_index)
    close = float(bars[-1]["close"]) if bars else None
    supports = sorted([z for z in zones if z["support_resistance_role"] == "SUPPORT" and close is not None and z["midpoint"] <= close], key=lambda x: close - x["midpoint"])
    resistances = sorted([z for z in zones if z["support_resistance_role"] == "RESISTANCE" and close is not None and z["midpoint"] >= close], key=lambda x: x["midpoint"] - close)
    return {"schema": "sr_context", "timeframe": timeframe, "timestamp": int(as_of), "zones": zones, "nearest_support": supports[0] if supports else None, "nearest_resistance": resistances[0] if resistances else None, "provenance": {"as_of_timestamp": int(as_of), "future_bars_excluded": True}}
