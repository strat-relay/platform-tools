"""No-lookahead structural research primitives.

This module is intentionally research-only.  It has no runner, bridge, broker,
or production-strategy imports.  Every detector accepts an ``as_of`` boundary
and only consumes candles whose close is at or before that boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Iterable, Sequence


TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    timeframe: str
    spread_points: float | None = None
    digits: int | None = None
    point: float | None = None

    @property
    def close_timestamp(self) -> datetime:
        return _utc(self.timestamp) + timedelta(minutes=TIMEFRAME_MINUTES[self.timeframe])

    def closed_by(self, as_of: datetime) -> bool:
        return self.close_timestamp <= _utc(as_of)


@dataclass(frozen=True)
class Swing:
    timestamp: datetime
    price: float
    kind: str
    index: int


@dataclass(frozen=True)
class StructureMap:
    timeframe: str
    as_of: datetime
    swings: tuple[Swing, ...]
    state: str
    direction: str
    bos: str | None
    choch: str | None
    last_high: Swing | None
    last_low: Swing | None


class H1Scenario(str, Enum):
    TREND_CONTINUATION = "HTF_CONTINUATION"
    COMPRESSION_BREAKOUT = "COMPRESSION_BREAKOUT"
    BREAK_RETEST = "BREAK_RETEST"
    STRUCTURAL_REVERSAL = "STRUCTURAL_REVERSAL"
    LIQUIDITY_RECLAIM = "LIQUIDITY_RECLAIM"
    UNCLASSIFIED = "UNCLASSIFIED"


class Compatibility(str, Enum):
    ALIGNED = "ALIGNED"
    NEUTRAL = "NEUTRAL"
    OPPOSING = "OPPOSING"
    TRANSITION_ACCEPTABLE = "TRANSITION_ACCEPTABLE"


@dataclass(frozen=True)
class H4Context:
    direction: str
    state: str
    compatibility: Compatibility


@dataclass(frozen=True)
class H1Context:
    structure_direction: str
    trend_state: str
    scenario: H1Scenario
    scenario_direction: str | None
    compression: dict
    compatibility: Compatibility


@dataclass(frozen=True)
class M15Setup:
    parent_h1_context: str
    confirmation_type: str
    confirmation_direction: str
    timestamp: datetime


def compatibility(context_direction: str, trade_direction: str) -> Compatibility:
    expected = "BULLISH" if trade_direction in {"LONG", "BULLISH"} else "BEARISH" if trade_direction in {"SHORT", "BEARISH"} else "NEUTRAL"
    if context_direction == "NEUTRAL" or expected == "NEUTRAL":
        return Compatibility.NEUTRAL
    return Compatibility.ALIGNED if context_direction == expected else Compatibility.OPPOSING


def classify_h4_context(structure: StructureMap, trade_direction: str) -> H4Context:
    return H4Context(structure.direction, structure.state, compatibility(structure.direction, trade_direction))


def classify_h1_context(structure: StructureMap, compression: dict, *, scenario: H1Scenario | None = None,
                        trade_direction: str | None = None, m15_confirmation: dict | None = None) -> H1Context:
    scenario = scenario or H1Scenario.UNCLASSIFIED
    scenario_direction = None
    if scenario in {H1Scenario.TREND_CONTINUATION, H1Scenario.COMPRESSION_BREAKOUT}:
        scenario_direction = structure.direction
    elif m15_confirmation:
        scenario_direction = m15_confirmation.get("direction")
    comp = compatibility(structure.direction, trade_direction) if trade_direction else Compatibility.NEUTRAL
    if comp == Compatibility.OPPOSING and m15_confirmation and scenario in {H1Scenario.STRUCTURAL_REVERSAL, H1Scenario.BREAK_RETEST, H1Scenario.LIQUIDITY_RECLAIM}:
        comp = Compatibility.TRANSITION_ACCEPTABLE
    return H1Context(structure.direction, structure.state, scenario, scenario_direction, compression, comp)


def make_m15_setup(h1_context: H1Context, confirmation: dict | None) -> M15Setup | None:
    if not confirmation:
        return None
    return M15Setup(h1_context.structure_direction, confirmation["type"], confirmation["direction"], confirmation["timestamp"])


def _eligible(bars: Iterable[Bar], as_of: datetime) -> list[Bar]:
    return sorted((b for b in bars if b.closed_by(as_of)), key=lambda b: _utc(b.timestamp))


def confirmed_swings(bars: Sequence[Bar], as_of: datetime, left: int = 2, right: int = 2) -> tuple[Swing, ...]:
    """Return pivots confirmed strictly from closed candles only."""
    eligible = _eligible(bars, as_of)
    if left < 1 or right < 1:
        raise ValueError("left/right confirmation windows must be positive")
    result: list[Swing] = []
    for i in range(left, len(eligible) - right):
        center = eligible[i]
        left_bars = eligible[i-left:i]
        right_bars = eligible[i+1:i+right+1]
        if center.high > max(x.high for x in (*left_bars, *right_bars)):
            result.append(Swing(_utc(center.timestamp), center.high, "HIGH", i))
        if center.low < min(x.low for x in (*left_bars, *right_bars)):
            result.append(Swing(_utc(center.timestamp), center.low, "LOW", i))
    return tuple(result)


def build_structure_map(bars: Sequence[Bar], as_of: datetime, left: int = 2, right: int = 2) -> StructureMap:
    eligible = _eligible(bars, as_of)
    swings = confirmed_swings(eligible, as_of, left, right)
    highs = [s for s in swings if s.kind == "HIGH"]
    lows = [s for s in swings if s.kind == "LOW"]
    last_high, last_low = (highs[-1] if highs else None), (lows[-1] if lows else None)
    hh = len(highs) >= 2 and highs[-1].price > highs[-2].price
    hl = len(lows) >= 2 and lows[-1].price > lows[-2].price
    lh = len(highs) >= 2 and highs[-1].price < highs[-2].price
    ll = len(lows) >= 2 and lows[-1].price < lows[-2].price
    if hh and hl:
        direction, state = "BULLISH", "UPTREND"
    elif lh and ll:
        direction, state = "BEARISH", "DOWNTREND"
    else:
        direction, state = "NEUTRAL", "RANGE_OR_TRANSITION"
    last_close = eligible[-1].close if eligible else None
    bos = "BULLISH" if last_high and last_close > last_high.price else ("BEARISH" if last_low and last_close < last_low.price else None)
    prior_direction = "BULLISH" if hh or hl else ("BEARISH" if lh or ll else "NEUTRAL")
    choch = bos if bos and prior_direction not in ("NEUTRAL", bos) else None
    return StructureMap(eligible[0].timeframe if eligible else "", _utc(as_of), swings, state, direction, bos, choch, last_high, last_low)


def _atr(bars: Sequence[Bar], period: int = 14) -> float:
    if not bars:
        return 0.0
    sample = list(bars)[-period:]
    return sum(b.high - b.low for b in sample) / len(sample)


def compression_geometry(bars: Sequence[Bar], as_of: datetime, tolerance_atr: float = 0.25) -> dict:
    """Describe converging confirmed swing lines without requiring hand-drawn lines."""
    eligible = _eligible(bars, as_of)
    swings = confirmed_swings(eligible, as_of)
    highs, lows = [s for s in swings if s.kind == "HIGH"], [s for s in swings if s.kind == "LOW"]
    atr = _atr(eligible)
    tol = atr * tolerance_atr
    def slope(points: list[Swing]) -> float | None:
        if len(points) < 2: return None
        a, b = points[-2:]
        dt = max((b.timestamp - a.timestamp).total_seconds() / 60.0, 1.0)
        return (b.price - a.price) / dt
    support = min((x.price for x in lows[-3:]), default=None)
    resistance = max((x.price for x in highs[-3:]), default=None)
    support_tests = sum(abs(b.low - support) <= tol for b in eligible) if support is not None else 0
    resistance_tests = sum(abs(b.high - resistance) <= tol for b in eligible) if resistance is not None else 0
    width = (resistance - support) if support is not None and resistance is not None else None
    return {"support": support, "resistance": resistance, "support_tests": support_tests,
            "resistance_tests": resistance_tests, "high_slope_per_minute": slope(highs),
            "low_slope_per_minute": slope(lows), "width": width,
            "width_atr": width / atr if width is not None and atr else None,
            "atr": atr, "bearish": bool(slope(highs) is not None and slope(highs) < 0 and support_tests >= 2),
            "bullish": bool(slope(lows) is not None and slope(lows) > 0 and resistance_tests >= 2)}


def support_resistance_levels(bars: Sequence[Bar], as_of: datetime, tolerance_atr: float = 0.25) -> list[dict]:
    eligible = _eligible(bars, as_of); swings = confirmed_swings(eligible, as_of); atr = _atr(eligible)
    tolerance = atr * tolerance_atr
    levels: list[dict] = []
    for kind in ("SUPPORT", "RESISTANCE"):
        points = [s.price for s in swings if (kind == "SUPPORT" and s.kind == "LOW") or (kind == "RESISTANCE" and s.kind == "HIGH")]
        for price in points:
            existing = next((x for x in levels if x["kind"] == kind and abs(x["price"] - price) <= tolerance), None)
            if existing: existing["test_count"] += 1
            else: levels.append({"kind": kind, "price": price, "test_count": 1, "tolerance": tolerance})
    for level in levels:
        level["distance_price"] = abs(eligible[-1].close - level["price"]) if eligible else None
        level["distance_atr"] = level["distance_price"] / atr if atr else None
        level["classification"] = ("MULTIPLE_TEST_LEVEL" if level["test_count"] >= 2 else "FRESH_LEVEL")
    return levels


def classify_h1_scenario(h4: StructureMap, h1: StructureMap, h1_compression: dict,
                         *, broke_level: bool = False, retested: bool = False,
                         reversal_confirmation: bool = False, liquidity_reclaim: bool = False) -> H1Scenario:
    if liquidity_reclaim: return H1Scenario.LIQUIDITY_RECLAIM
    if reversal_confirmation or h1.choch: return H1Scenario.STRUCTURAL_REVERSAL
    if broke_level and retested: return H1Scenario.BREAK_RETEST
    if broke_level and (h1_compression.get("bearish") or h1_compression.get("bullish")):
        return H1Scenario.COMPRESSION_BREAKOUT
    if h4.direction == h1.direction and h1.direction in {"BULLISH", "BEARISH"}:
        return H1Scenario.TREND_CONTINUATION
    return H1Scenario.UNCLASSIFIED


def m15_confirmations(bars: Sequence[Bar], as_of: datetime, level: float | None = None,
                      displacement_atr: float = 0.5) -> list[dict]:
    eligible = _eligible(bars, as_of); atr = _atr(eligible); out=[]
    for i, b in enumerate(eligible):
        body = abs(b.close-b.open); direction = "BULLISH" if b.close>b.open else ("BEARISH" if b.close<b.open else "NEUTRAL")
        if body >= displacement_atr * atr and direction != "NEUTRAL":
            out.append({"type": "DISPLACEMENT", "timestamp": _utc(b.timestamp), "direction": direction, "body": body, "atr": atr})
        if level is not None and ((b.close > level and b.open <= level) or (b.close < level and b.open >= level)):
            out.append({"type": "HTF_LEVEL_CLOSE", "timestamp": _utc(b.timestamp), "direction": direction, "level": level})
        if i and level is not None:
            prev=eligible[i-1]
            if (prev.close < level <= b.close) or (prev.close > level >= b.close):
                out.append({"type": "BREAK_RETEST_OR_CROSS", "timestamp": _utc(b.timestamp), "direction": direction, "level": level})
    return out


def sniper_trigger(m5: Sequence[Bar], as_of: datetime, direction: str, family: str,
                   level: float | None = None) -> dict | None:
    """Return the first completed M5 trigger after context confirmation."""
    bars = _eligible(m5, as_of)
    for i, b in enumerate(bars):
        prev = bars[i-1] if i else None
        bullish = direction == "LONG"
        if family in {"MICRO_BOS", "MICRO_CHOCH"} and prev and ((bullish and b.close > prev.high) or (not bullish and b.close < prev.low)):
            return {"family": family, "timestamp": _utc(b.timestamp), "entry": b.close, "stop_anchor": b.low if bullish else b.high, "confirmation": "CLOSE_BREAK"}
        if family == "BREAK_RETEST" and level is not None and prev and ((bullish and prev.close > level and b.low <= level and b.close > level) or (not bullish and prev.close < level and b.high >= level and b.close < level)):
            return {"family": family, "timestamp": _utc(b.timestamp), "entry": b.close, "stop_anchor": b.low if bullish else b.high, "confirmation": "RETEST_HOLD"}
        if family in {"REJECTION_WICK", "ENGULFING", "CLOSE_RECLAIM", "SWEEP_RECLAIM",
                      "TOUCH_CLOSE_HOLD", "SHALLOW_RETRACEMENT", "PSYCHOLOGICAL_LEVEL",
                      "MICRO_LOWER_HIGH", "MICRO_HIGHER_LOW"} and prev:
            engulf = (bullish and b.close > b.open and b.open <= prev.close and b.close >= prev.open) or (not bullish and b.close < b.open and b.open >= prev.close and b.close <= prev.open)
            reclaim = level is not None and ((bullish and b.low <= level < b.close) or (not bullish and b.high >= level > b.close))
            wick_rejection = level is not None and ((bullish and b.low <= level < b.close and b.close > b.open) or (not bullish and b.high >= level > b.close and b.close < b.open))
            lower_high = not bullish and b.high < prev.high and b.close < b.open
            higher_low = bullish and b.low > prev.low and b.close > b.open
            if ((family == "ENGULFING" and engulf) or
                (family in {"CLOSE_RECLAIM", "SWEEP_RECLAIM", "TOUCH_CLOSE_HOLD",
                            "SHALLOW_RETRACEMENT", "PSYCHOLOGICAL_LEVEL"} and reclaim) or
                (family == "REJECTION_WICK" and wick_rejection) or
                (family == "MICRO_LOWER_HIGH" and lower_high) or
                (family == "MICRO_HIGHER_LOW" and higher_low)):
                return {"family": family, "timestamp": _utc(b.timestamp), "entry": b.close, "stop_anchor": b.low if bullish else b.high, "confirmation": "CLOSE"}
    return None


def candidate_from_context(*, h4: StructureMap | None, h1: StructureMap | None,
                           scenario: H1Scenario | None, m15_confirmation: dict | None,
                           m5: Sequence[Bar], as_of: datetime, direction: str,
                           family: str, level: float | None = None) -> dict | None:
    """Gate the M5 primitive behind the complete structural state machine."""
    if h4 is None or h1 is None or scenario in (None, H1Scenario.UNCLASSIFIED):
        return None
    if not m15_confirmation or m15_confirmation.get("direction") != ("BULLISH" if direction == "LONG" else "BEARISH"):
        return None
    trigger = sniper_trigger(m5, as_of, direction, family, level)
    if trigger is None:
        return None
    return {"scenario": scenario.value, "h4_direction": h4.direction,
            "h1_direction": h1.direction, "m15_confirmation": m15_confirmation,
            "m5_trigger": trigger}


def assert_completed_alignment(decision_timestamp: datetime, *, h4: Bar, h1: Bar,
                               m15: Bar, m5: Bar) -> None:
    """Raise if any source candle is partial at the decision boundary."""
    decision = _utc(decision_timestamp)
    for name, bar in (("H4", h4), ("H1", h1), ("M15", m15), ("M5", m5)):
        if not bar.closed_by(decision):
            raise AssertionError(f"partial {name} candle used at {decision.isoformat()}")


def ema(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or not values:
        return None
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def ema_context(bars: Sequence[Bar], as_of: datetime, periods: Sequence[int] = (20, 50, 100, 200)) -> dict:
    eligible = _eligible(bars, as_of)
    closes = [b.close for b in eligible]
    current = closes[-1] if closes else None
    result = {f"ema{p}": ema(closes[-p * 2:] if len(closes) >= p else closes, p) for p in periods}
    result["price"] = current
    result["price_relative"] = {str(p): ("ABOVE" if current is not None and result[f"ema{p}"] is not None and current > result[f"ema{p}"] else "BELOW" if current is not None and result[f"ema{p}"] is not None else "UNKNOWN") for p in periods}
    return result


def psychological_levels(price: float, pip: float, steps_pips: Sequence[int] = (50, 100)) -> dict:
    return {f"nearest_{step}pip": {"level": round(round(price / (pip * step)) * pip * step, 10),
                                   "distance_pips": abs(price - round(price / (pip * step)) * pip * step) / pip}
            for step in steps_pips}


def candle_patterns(previous: Bar | None, current: Bar) -> dict:
    body = abs(current.close - current.open)
    rng = max(current.high - current.low, 1e-12)
    upper = current.high - max(current.open, current.close)
    lower = min(current.open, current.close) - current.low
    return {
        "bullish_engulfing": bool(previous and current.close > current.open and current.open <= previous.close and current.close >= previous.open),
        "bearish_engulfing": bool(previous and current.close < current.open and current.open >= previous.close and current.close <= previous.open),
        "rejection_wick": max(upper, lower) >= body * 2,
        "strong_body_close": body / rng >= 0.65,
        "inside_bar": bool(previous and current.high <= previous.high and current.low >= previous.low),
        "outside_bar": bool(previous and current.high >= previous.high and current.low <= previous.low),
    }


def structural_targets(levels: Sequence[dict], entry: float, direction: str, stop: float,
                       fixed_r: Sequence[float] = (0.75, 1.0, 1.5, 2.0)) -> dict:
    candidates = [float(x["price"]) for x in levels if (direction == "LONG" and x["price"] > entry) or (direction == "SHORT" and x["price"] < entry)]
    structural = (min(candidates) if direction == "LONG" and candidates else max(candidates) if direction == "SHORT" and candidates else None)
    risk = abs(entry-stop)
    return {"structural_target": structural, "fixed_r_targets": [entry + r*risk if direction == "LONG" else entry-r*risk for r in fixed_r],
            "structural_target_R": abs(structural-entry)/risk if structural is not None and risk else None}


def pip_size(symbol: str, digits: int, point: float) -> float:
    """Normalize broker-suffix and JPY/non-JPY symbols using quote digits."""
    return point * (10.0 if digits in (3, 5) else 1.0)


def fill_cost(symbol: str, *, fill_timestamp: datetime, spread_timestamp: datetime | None,
              bid: float | None, ask: float | None, stop_distance_price: float,
              digits: int, point: float) -> dict:
    if spread_timestamp is None or _utc(spread_timestamp) != _utc(fill_timestamp):
        return {"status": "FILL_COST_UNAVAILABLE", "spread_cost_R": None}
    if bid is None or ask is None or ask < bid or stop_distance_price <= 0:
        return {"status": "FILL_COST_UNAVAILABLE", "spread_cost_R": None}
    pp = pip_size(symbol, digits, point); spread_price = ask-bid
    spread_pips = spread_price/pp; stop_pips = stop_distance_price/pp
    return {"status": "OK", "spread_timestamp": _utc(spread_timestamp), "fill_timestamp": _utc(fill_timestamp),
            "spread_pips": spread_pips, "stop_pips": stop_pips,
            "spread_cost_R": spread_price/stop_distance_price, "spread_double_counted": False,
            "pip_conversion_valid": True}


def as_record(value) -> dict:
    return asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
