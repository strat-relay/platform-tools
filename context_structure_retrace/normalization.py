from __future__ import annotations

from typing import Any


def normalization_audit(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable audit of dimensionality for Phase 2 features."""
    rows = [
        ("sr.zone_width", "zone_width_atr", "ATR normalized", False),
        ("sr.distance", "distance_from_current_price_atr", "ATR normalized", False),
        ("sr.reaction_magnitude", "reactions[].reaction_magnitude_atr", "ATR normalized; raw price retained for audit", False),
        ("sr.wick_penetration", "reactions[].wick_penetration_atr", "ATR normalized; raw price retained for audit", False),
        ("sr.body_penetration", "reactions[].body_penetration_atr", "ATR normalized; raw price retained for audit", False),
        ("sr.rejection", "reactions[].atr_normalized_rejection", "ATR normalized", False),
        ("trendline.distance", "current_distance_atr", "ATR normalized", False),
        ("trendline.penetration", "max_penetration_atr", "ATR normalized", False),
        ("trendline.slope", "slope_atr_per_hour", "ATR/time normalized", False),
        ("channel.width", "width_atr", "ATR normalized", False),
        ("channel.slope", "slope_atr_per_hour", "ATR/time normalized", False),
        ("channel.similarity", "upper/lower distance divided by ATR", "ATR normalized", False),
        ("ema.distance", "normalized_price_distance_atr", "ATR normalized", False),
        ("ema.separation", "pair_separation[].normalized_atr", "ATR normalized", False),
        ("retracement.depth", "range/body/ATR fractions", "percentage and ATR normalized", False),
        ("meaningful_departure", "departure > 2 x ATR-relative tolerance", "ATR normalized", False),
        ("rejection.magnitude", "atr_normalized_rejection", "ATR normalized", False),
        ("market.price", "raw OHLC retained for provenance/execution", "instrument metadata dependent", False),
    ]
    return {
        "schema": "normalization_audit",
        "instrument_independent": True,
        "fields": [{"feature": a, "representation": b, "category": c, "absolute_unit_issue": d} for a, b, c, d in rows],
        "absolute_unit_issues": [],
        "policy": "absolute prices are retained only as descriptive market values; structural comparisons use ATR, percentages, time, R, or broker metadata",
        "provenance": {"as_of_timestamp": snapshot.get("timestamp"), "future_outcomes_not_used": True},
    }
