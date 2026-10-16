"""Versioned, pure Price Action ingredients shared by research evaluators."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import MarketEvent


PRIMITIVE_IMPLEMENTATIONS = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "price_action.engulfing@1": "strict-body-engulfing",
    "risk.structural_stop@1": "retest-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}


@dataclass(frozen=True)
class Candle:
    index: int
    open_timestamp: int
    close_timestamp: int
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_event(cls, index: int, event: MarketEvent) -> "Candle":
        return cls(index, event.open_timestamp, event.close_timestamp, event.open,
                   event.high, event.low, event.close)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Swing:
    kind: str
    bar_index: int
    price: float
    event_time: int
    known_at: int

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SRLevel:
    kind: str
    price: float
    source_swings: tuple[int, ...]
    known_at: int
    expires_at: int

    def payload(self) -> dict[str, Any]:
        return asdict(self) | {"source_swings": list(self.source_swings)}


def validate_candle(candle: Candle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close)
    if any(value != value or value in (float("inf"), float("-inf")) for value in values):
        raise ValueError("invalid OHLC value")
    if candle.close_timestamp <= candle.open_timestamp:
        raise ValueError("timestamps must be increasing")
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        raise ValueError("inconsistent OHLC")


def confirmed_swings(candles: tuple[Candle, ...], *, left: int, right: int) -> tuple[Swing, ...]:
    """Strict pivots; center i is known only at the close of i + right."""
    if left <= 0 or right <= 0:
        raise ValueError("swing windows must be positive")
    result: list[Swing] = []
    for center in range(left, len(candles) - right):
        current = candles[center]
        before = candles[center - left:center]
        after = candles[center + 1:center + right + 1]
        if current.high > max(x.high for x in before + after):
            result.append(Swing("RESISTANCE", center, current.high, current.close_timestamp,
                                after[-1].close_timestamp))
        if current.low < min(x.low for x in before + after):
            result.append(Swing("SUPPORT", center, current.low, current.close_timestamp,
                                after[-1].close_timestamp))
    return tuple(result)


def construct_levels(swings: tuple[Swing, ...], *, as_of: int, tolerance: float,
                      lookback: int, max_age: int) -> tuple[SRLevel, ...]:
    if tolerance <= 0 or lookback <= 0 or max_age <= 0:
        raise ValueError("level parameters must be positive")
    usable = [s for s in swings if s.known_at <= as_of and as_of - s.known_at <= max_age * 3600]
    clusters: list[list[Swing]] = []
    for swing in usable[-lookback:]:
        cluster = next((group for group in clusters
                        if group[0].kind == swing.kind and abs(group[-1].price - swing.price) <= tolerance), None)
        if cluster is None:
            clusters.append([swing])
        else:
            cluster.append(swing)
    return tuple(sorted((SRLevel(group[0].kind, sum(s.price for s in group) / len(group),
                                  tuple(s.bar_index for s in group), max(s.known_at for s in group),
                                  as_of + max_age) for group in clusters),
                        key=lambda level: (level.kind, level.price, level.known_at)))


def engulfing_confirmation(previous: Candle, candle: Candle, *, direction: str) -> bool:
    """Strict body engulfing: doji and wick-only overlap never qualify."""
    if direction == "LONG":
        return (previous.close < previous.open and candle.close > candle.open and
                candle.open <= previous.close and candle.close >= previous.open)
    if direction == "SHORT":
        return (previous.close > previous.open and candle.close < candle.open and
                candle.open >= previous.close and candle.close <= previous.open)
    raise ValueError("direction must be LONG or SHORT")


def star_reversal_confirmation(first: Candle, middle: Candle, third: Candle, *,
                               minimum_first_body: float, minimum_third_body: float,
                               middle_body_max_fraction: float,
                               recovery_fraction: float, direction: str) -> bool:
    """Pure three-candle star geometry used by the V1 reversal evaluator."""
    first_body = abs(first.close - first.open)
    middle_body = abs(middle.close - middle.open)
    third_body = abs(third.close - third.open)
    if first_body < minimum_first_body or third_body < minimum_third_body:
        return False
    if middle.high <= middle.low or middle_body > first_body * middle_body_max_fraction:
        return False
    if direction == "LONG":
        recovery = first.close + (first.open - first.close) * recovery_fraction
        return first.close < first.open and third.close > third.open and third.close > recovery
    if direction == "SHORT":
        recovery = first.close - (first.close - first.open) * recovery_fraction
        return first.close > first.open and third.close < third.open and third.close < recovery
    raise ValueError("direction must be LONG or SHORT")


def pin_bar_confirmation(candle: Candle, *, minimum_range: float, minimum_wick_body_ratio: float,
                         maximum_opposite_wick_body_ratio: float, minimum_close_location: float,
                         direction: str) -> bool:
    """Pure directional rejection geometry; zero body/range never qualifies."""
    total_range = candle.high - candle.low
    body = abs(candle.close - candle.open)
    if total_range <= 0 or body <= 0 or total_range < minimum_range:
        return False
    lower_wick = min(candle.open, candle.close) - candle.low
    upper_wick = candle.high - max(candle.open, candle.close)
    if direction == "LONG":
        return (candle.close > candle.open and lower_wick / body >= minimum_wick_body_ratio
                and upper_wick / body <= maximum_opposite_wick_body_ratio
                and (candle.close - candle.low) / total_range >= minimum_close_location)
    if direction == "SHORT":
        return (candle.close < candle.open and upper_wick / body >= minimum_wick_body_ratio
                and lower_wick / body <= maximum_opposite_wick_body_ratio
                and (candle.high - candle.close) / total_range >= minimum_close_location)
    raise ValueError("direction must be LONG or SHORT")


def structural_stop(candle: Candle, *, direction: str, buffer: float) -> float:
    if buffer < 0:
        raise ValueError("stop buffer cannot be negative")
    return candle.low - buffer if direction == "LONG" else candle.high + buffer


def opposing_target(levels: tuple[SRLevel, ...], *, direction: str, entry: float,
                    as_of: int) -> SRLevel | None:
    opposing = "RESISTANCE" if direction == "LONG" else "SUPPORT"
    eligible = [level for level in levels if level.kind == opposing and level.known_at <= as_of and
                ((direction == "LONG" and level.price > entry) or (direction == "SHORT" and level.price < entry))]
    return min(eligible, key=lambda level: abs(level.price - entry)) if eligible else None
