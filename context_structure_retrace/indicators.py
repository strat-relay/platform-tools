from __future__ import annotations

from itertools import combinations
from typing import Any


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1 - alpha) * out[-1])
    return out


def atr(bars: list[dict[str, Any]], period: int = 14) -> list[float]:
    if not bars:
        return []
    trs: list[float] = []
    previous = None
    for bar in bars:
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        trs.append(max(high - low, abs(high - previous), abs(low - previous)) if previous is not None else high - low)
        previous = close
    # Equivalent to the original rolling-window calculation, but linear-time.
    # This preserves every ATR value while keeping historical Phase 2/3
    # ledgers tractable when thousands of causal snapshots are generated.
    out: list[float] = []
    rolling = 0.0
    for i, value in enumerate(trs):
        rolling += value
        if i >= period:
            rolling -= trs[i - period]
        out.append(rolling / min(period, i + 1))
    return out


def _direction(value: float, epsilon: float = 1e-12) -> str:
    return "UP" if value > epsilon else "DOWN" if value < -epsilon else "FLAT"


def ema_context(completed_bars: list[dict[str, Any]], forming: dict[str, Any] | None, as_of: int, timeframe: str) -> dict[str, Any]:
    closes = [float(x["close"]) for x in completed_bars]
    atr_values = atr(completed_bars)
    atr_now = atr_values[-1] if atr_values else None
    values: dict[str, float | None] = {}
    forming_values: dict[str, float | None] = {}
    series: dict[int, list[float]] = {}
    for period in (20, 50, 100, 200):
        series[period] = ema(closes, period)
        values[str(period)] = series[period][-1] if series[period] else None
        with_forming = closes + ([float(forming["close"])] if forming else [])
        fseries = ema(with_forming, period)
        forming_values[str(period)] = fseries[-1] if forming and fseries else values[str(period)]

    latest_close = float((forming or (completed_bars[-1] if completed_bars else {"close": 0})) ["close"]) if (forming or completed_bars) else None
    slopes = {}
    distances = {}
    normalized_distances = {}
    touches = {}
    for period in (20, 50, 100, 200):
        s = series[period]
        slope = (s[-1] - s[max(0, len(s) - 4)]) / max(1, min(3, len(s) - 1)) if len(s) > 1 else None
        slopes[str(period)] = {"value_per_candle": slope, "normalized_atr_per_candle": slope / atr_now if slope is not None and atr_now else None, "direction": _direction(slope or 0)}
        distance = latest_close - values[str(period)] if latest_close is not None and values[str(period)] is not None else None
        distances[str(period)] = distance
        normalized_distances[str(period)] = distance / atr_now if distance is not None and atr_now else None
        touched = [x for x in completed_bars[-20:] if float(x["low"]) <= float(values[str(period)]) <= float(x["high"])] if values[str(period)] is not None else []
        touches[str(period)] = {"count_last_20": len(touched), "timestamps": [int(x["time"]) for x in touched]}

    pair_separation = {}
    for a, b in combinations((20, 50, 100, 200), 2):
        key = f"{a}_{b}"
        sep = values[str(a)] - values[str(b)] if values[str(a)] is not None and values[str(b)] is not None else None
        pair_separation[key] = {"price": sep, "normalized_atr": sep / atr_now if sep is not None and atr_now else None}
    signs = [values[str(p)] for p in (20, 50, 100, 200)]
    ordering = "UNKNOWN" if any(x is None for x in signs) else "BULLISH_STACK" if signs == sorted(signs, reverse=True) else "BEARISH_STACK" if signs == sorted(signs) else "MIXED"

    crosses = {}
    for a, b in combinations((20, 50, 100, 200), 2):
        key = f"{a}_{b}"
        delta = [x - y for x, y in zip(series[a], series[b])]
        events = []
        for i in range(1, len(delta)):
            if delta[i - 1] <= 0 < delta[i]: events.append({"direction": "UP", "timestamp": int(completed_bars[i]["time"])})
            if delta[i - 1] >= 0 > delta[i]: events.append({"direction": "DOWN", "timestamp": int(completed_bars[i]["time"])})
        last = events[-1] if events else None
        crosses[key] = {"events": events, "last": last, "candles_since_cross": len(completed_bars) - 1 - next((i for i, x in enumerate(completed_bars) if last and int(x["time"]) == last["timestamp"]), len(completed_bars) - 1) if last else None}

    compression = None
    if all(pair_separation[k]["normalized_atr"] is not None for k in pair_separation):
        total = sum(abs(pair_separation[k]["normalized_atr"]) for k in pair_separation)
        prior_closes = closes[-5:]
        prior_atr = atr(completed_bars[:-5])[-1] if len(completed_bars) > 19 else None
        compression = {"pairwise_total_normalized_separation": total, "state": "COMPRESSED" if total < 2 else "EXPANDED" if prior_atr and atr_now > prior_atr * 1.2 else "NORMAL"}

    return {
        "schema": "ema_context",
        "timeframe": timeframe,
        "timestamp": int(as_of),
        "ema_values_completed": values,
        "ema_values_forming": forming_values,
        "price": latest_close,
        "price_distance": distances,
        "normalized_price_distance_atr": normalized_distances,
        "slopes": slopes,
        "ordering": ordering,
        "pair_separation": pair_separation,
        "compression_expansion": compression,
        "crosses": crosses,
        "touches_rejections": touches,
        "atr": atr_now,
        "provenance": {"as_of_timestamp": int(as_of), "timeframe": timeframe, "completed_bars_used": len(completed_bars), "forming_close_used": bool(forming), "future_bars_excluded": True},
    }
