from __future__ import annotations

from typing import Any


def measure_retracement(event_bar: dict[str, Any], future_bars: list[dict[str, Any]], direction: str, atr_value: float | None, ema_values: dict[str, float | None] | None = None, zones: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    open_, high, low, close = [float(event_bar[k]) for k in ("open", "high", "low", "close")]
    body = abs(close - open_); rng = max(high - low, 1e-12); sign = 1 if direction == "UP" else -1
    extreme = high if sign > 0 else low
    max_pullback = 0.0; before_new_extreme = []
    for index, bar in enumerate(future_bars, 1):
        pullback = (close - float(bar["low"])) if sign > 0 else (float(bar["high"]) - close)
        max_pullback = max(max_pullback, pullback)
        moved = float(bar["high"]) > extreme if sign > 0 else float(bar["low"]) < extreme
        if moved:
            before_new_extreme.append(index)
            break
    return {
        "event_range": rng, "event_body": body,
        "maximum_retracement_price": max_pullback,
        "maximum_retracement_range_pct": max_pullback / rng,
        "maximum_retracement_body_pct": max_pullback / body if body else None,
        "maximum_retracement_atr": max_pullback / atr_value if atr_value else None,
        "retracement_before_new_extreme": bool(before_new_extreme),
        "candles_until_new_extreme": before_new_extreme[0] if before_new_extreme else None,
        "duration_until_new_extreme_minutes": before_new_extreme[0] * 5 if before_new_extreme else None,
        "levels_observed": {"20_pct_range": rng * .20, "50_pct_range": rng * .50, "20_pct_body": body * .20, "50_pct_body": body * .50},
        "ema_touched_during_retracement": {k: any(float(b["low"]) <= float(v) <= float(b["high"]) for b in future_bars) if v is not None else False for k, v in (ema_values or {}).items()},
        "sr_touched_during_retracement": [z["level_id"] for z in (zones or []) if any(float(b["low"]) <= float(z["zone_high"]) and float(b["high"]) >= float(z["zone_low"]) for b in future_bars)],
        "provenance": {"future_path_used_for_label_only": True, "not_available_to_feature_generation": True},
    }
