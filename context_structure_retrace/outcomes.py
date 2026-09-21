from __future__ import annotations

from typing import Any


def future_outcome_labels(event_bar: dict[str, Any], future_bars: list[dict[str, Any]], direction: str, atr_value: float | None, horizons: tuple[int, ...] = (1, 2, 3, 6, 12, 24)) -> dict[str, Any]:
    close = float(event_bar["close"]); rng = max(float(event_bar["high"]) - float(event_bar["low"]), 1e-12); sign = 1 if direction == "UP" else -1
    labels = {}
    for horizon in horizons:
        path = future_bars[:horizon]
        if not path:
            labels[str(horizon)] = None; continue
        up = max(float(x["high"]) - close for x in path)
        down = max(close - float(x["low"]) for x in path)
        net = (float(path[-1]["close"]) - close) * sign
        labels[str(horizon)] = {"mfe_favorable_price": max(0.0, up if sign > 0 else down), "mae_adverse_price": max(0.0, down if sign > 0 else up), "net_directional_price": net, "mfe_atr": max(0.0, up if sign > 0 else down) / atr_value if atr_value else None, "mae_atr": max(0.0, down if sign > 0 else up) / atr_value if atr_value else None, "net_range_units": net / rng}
    thresholds = {}
    for unit in (0.25, 0.50, 0.75, 1.0, 1.25):
        threshold = rng * unit
        hit = next((i for i, x in enumerate(future_bars, 1) if (float(x["high"]) - close >= threshold if sign > 0 else close - float(x["low"]) >= threshold)), None)
        thresholds[str(unit)] = {"threshold_price": threshold, "first_hit_candle": hit, "time_minutes": hit * 5 if hit else None}
    return {"schema": "future_outcome_labels", "horizons": labels, "thresholds": thresholds, "provenance": {"future_path_used_for_labels_only": True, "leakage_guard": "labels are not passed into attention or feature functions"}}
