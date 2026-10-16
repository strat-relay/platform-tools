"""Causal, pure library ingredients. No legacy implementations are replaced.

All input series are chronological completed candles bounded by the caller's
as-of cutoff. Events refer to close times; pivots carry separate known-at time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt, isfinite
from zoneinfo import ZoneInfo
from typing import Sequence

from strategy_backtest.models import MarketEvent

VERSION = 'library-primitives.v1'


@dataclass(frozen=True)
class Level:
    price: float
    kind: str
    event_at: int
    known_at: int


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    direction: int
    event_at: int
    known_at: int
    origin: str

    def intersects(self, bar: MarketEvent) -> bool:
        return bar.low <= self.high and bar.high >= self.low


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError('empty mean')
    return sum(values) / len(values)


def ema(values: Sequence[float], period: int) -> float:
    """SMA seed followed by alpha=2/(n+1), requires n observations."""
    if period < 1 or len(values) < period:
        raise ValueError('insufficient EMA warm-up')
    value = mean(values[:period])
    for price in values[period:]:
        value += 2 / (period + 1) * (price - value)
    return value


def atr(bars: Sequence[MarketEvent], period: int = 14) -> float:
    """SMA of last n true ranges. Caller excludes signal candle explicitly."""
    if period < 1 or len(bars) < period + 1:
        raise ValueError('insufficient ATR warm-up')
    return mean([max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
                 for a, b in zip(bars[-period-1:-1], bars[-period:])])


def rsi(values: Sequence[float], period: int = 14) -> float:
    """Cutler RSI: last n arithmetic-average gains/losses, neutral flat=50."""
    if len(values) < period + 1:
        raise ValueError('insufficient RSI warm-up')
    changes = [b-a for a, b in zip(values[-period-1:-1], values[-period:])]
    gains, losses = sum(max(c, 0) for c in changes), sum(max(-c, 0) for c in changes)
    return 50. if gains == losses == 0 else 100. if losses == 0 else 100-100/(1+gains/losses)


def bands(values: Sequence[float], period: int = 20, multiple: float = 2.) -> tuple[float, float, float]:
    values = values[-period:]
    if len(values) < period:
        raise ValueError('insufficient bands warm-up')
    center = mean(values)
    std = sqrt(mean([(v-center)**2 for v in values]))
    return center-multiple*std, center, center+multiple*std


def channel(bars: Sequence[MarketEvent], period: int) -> tuple[float, float]:
    if period < 1 or len(bars) < period:
        raise ValueError('insufficient channel warm-up')
    return min(b.low for b in bars[-period:]), max(b.high for b in bars[-period:])


def swings(bars: Sequence[MarketEvent], left: int = 2, right: int = 2) -> tuple[Level, ...]:
    out = []
    for i in range(left, len(bars)-right):
        b = bars[i]
        others = bars[i-left:i] + bars[i+1:i+right+1]
        if b.high > max(x.high for x in others):
            out.append(Level(b.high, 'HIGH', b.close_timestamp, bars[i+right].close_timestamp))
        if b.low < min(x.low for x in others):
            out.append(Level(b.low, 'LOW', b.close_timestamp, bars[i+right].close_timestamp))
    return tuple(out)


def trend(levels: Sequence[Level]) -> int:
    highs = [l.price for l in levels if l.kind == 'HIGH']
    lows = [l.price for l in levels if l.kind == 'LOW']
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 1
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return -1
    return 0


def engulfing(previous: MarketEvent, current: MarketEvent, direction: int) -> bool:
    return (previous.close < previous.open and current.close > current.open
            and current.open < previous.close and current.close > previous.open) if direction == 1 else (
            previous.close > previous.open and current.close < current.open
            and current.open > previous.close and current.close < previous.open)


def rejection(bar: MarketEvent, direction: int, ratio: float = 2.) -> bool:
    body, span = abs(bar.close-bar.open), bar.high-bar.low
    if body <= 0 or span <= 0:
        return False
    wick = min(bar.open, bar.close)-bar.low if direction == 1 else bar.high-max(bar.open, bar.close)
    opposite = bar.high-max(bar.open, bar.close) if direction == 1 else min(bar.open, bar.close)-bar.low
    location = (bar.close-bar.low)/span if direction == 1 else (bar.high-bar.close)/span
    return direction*(bar.close-bar.open) > 0 and wick >= ratio*body and opposite <= body and location >= .65


def displacement(bar: MarketEvent, prior_atr: float, direction: int, multiple: float = 1.2) -> bool:
    span = bar.high-bar.low
    return (span > 0 and direction*(bar.close-bar.open) >= multiple*prior_atr
            and abs(bar.close-bar.open)/span >= .65)


def fvgs(bars: Sequence[MarketEvent]) -> tuple[Zone, ...]:
    """Strict three-bar wick gap, known only on third close; no future mitigation."""
    out = []
    for a, _, c in zip(bars, bars[1:], bars[2:]):
        if c.low > a.high:
            out.append(Zone(a.high, c.low, 1, a.close_timestamp, c.close_timestamp, 'FVG'))
        if c.high < a.low:
            out.append(Zone(c.high, a.low, -1, a.close_timestamp, c.close_timestamp, 'FVG'))
    return tuple(out)


def active_zone(zone: Zone, bars: Sequence[MarketEvent], direction: int) -> bool:
    return not any((b.close < zone.low if direction == 1 else b.close > zone.high)
                   for b in bars if b.close_timestamp > zone.known_at)


def order_block(bars: Sequence[MarketEvent], direction: int, volatility: float) -> Zone | None:
    """Last opposing candle within 5 bars before displacement + prior-channel break.

    Entire wick range; available at displacement close. Explicit mechanical OB
    hypothesis, not a claim to cover every discretionary definition.
    """
    if len(bars) < 22 or not displacement(bars[-1], volatility, direction):
        return None
    lo, hi = channel(bars[:-1], 20)
    if not (bars[-1].close > hi if direction == 1 else bars[-1].close < lo):
        return None
    for b in reversed(bars[-6:-1]):
        if direction*(b.close-b.open) < 0:
            return Zone(b.low, b.high, direction, b.close_timestamp, bars[-1].close_timestamp, 'ORDER_BLOCK')
    return None


def session_bounds(timestamp: int, tz: str, start_minute: int, end_minute: int) -> tuple[int, int, str]:
    """Local-date session; cross-midnight membership anchors to prior start date.

    Windows must not use ambiguous/nonexistent local endpoints. Supported catalog
    endpoints avoid DST transition hours; unsupported endpoints fail explicitly.
    """
    local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(ZoneInfo(tz))
    date = local.date()
    if end_minute <= start_minute and local.hour*60+local.minute < end_minute:
        date -= timedelta(days=1)
    def endpoint(day, minute):
        naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
        first = naive.replace(tzinfo=ZoneInfo(tz), fold=0)
        second = naive.replace(tzinfo=ZoneInfo(tz), fold=1)
        if first.utcoffset() != second.utcoffset() or datetime.fromtimestamp(first.timestamp(), ZoneInfo(tz)).replace(tzinfo=None) != naive:
            raise ValueError('ambiguous/nonexistent session endpoint')
        return int(first.timestamp())
    end_date = date + timedelta(days=1) if end_minute <= start_minute else date
    return endpoint(date, start_minute), endpoint(end_date, end_minute), date.isoformat()


def session_range(bars: Sequence[MarketEvent], start: int, end: int, step: int) -> tuple[float, float] | None:
    selected = [b for b in bars if start <= b.open_timestamp < end and b.close_timestamp <= end]
    if not selected or [b.open_timestamp for b in selected] != list(range(start, end, step)):
        return None
    return min(b.low for b in selected), max(b.high for b in selected)


def vwap(bars: Sequence[MarketEvent]) -> float | None:
    """Tick-volume weighted typical price; absence/zero volume is unavailable."""
    volume = [b.provenance.get('tick_volume') for b in bars]
    if not bars or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0 for v in volume):
        return None
    return sum((b.high+b.low+b.close)/3*v for b, v in zip(bars, volume))/sum(volume)


def trendline(levels: Sequence[Level], direction: int, timestamp: int) -> float | None:
    """Line through latest two confirmed lows (up) or highs (down), no future fit."""
    points = [p for p in levels if p.kind == ('LOW' if direction == 1 else 'HIGH')]
    if len(points) < 2:
        return None
    a, b = points[-2:]
    if direction*(b.price-a.price) <= 0:
        return None
    return b.price+(b.price-a.price)*(timestamp-b.event_at)/(b.event_at-a.event_at)
