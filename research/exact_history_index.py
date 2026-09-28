"""Exact timestamp-bounded historical candle indexes for research replay."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Iterable

from context_structure_retrace.data import bar_end, timeframe_seconds


@dataclass(frozen=True)
class IndexedCandle:
    ordinal: int
    bar: dict[str, Any]
    open_timestamp: int
    close_timestamp: int
    low: float
    high: float
    close: float


class ExactHistoricalCandleIndex:
    """Sorted value indexes with exact prefix and chronological semantics."""

    VERSION = "exact-historical-candle-index-v1"

    def __init__(self, bars: Iterable[dict[str, Any]], timeframe: str) -> None:
        ordered = tuple(sorted(bars, key=lambda bar: int(bar["time"])))
        self.timeframe = timeframe
        self._items = tuple(
            IndexedCandle(i, bar, int(bar["time"]), int(bar_end(bar, timeframe)), float(bar["low"]), float(bar["high"]), float(bar["close"]))
            for i, bar in enumerate(ordered)
        )
        self._by_low = tuple(sorted(self._items, key=lambda item: (item.low, item.ordinal)))
        self._by_high = tuple(sorted(self._items, key=lambda item: (item.high, item.ordinal)))
        self._by_close = tuple(sorted(self._items, key=lambda item: (item.close, item.ordinal)))
        self._lows = tuple(item.low for item in self._by_low)
        self._highs = tuple(item.high for item in self._by_high)
        self._closes = tuple(item.close for item in self._by_close)

    def _prefix(self, item: IndexedCandle, as_of: int) -> bool:
        return item.close_timestamp <= int(as_of)

    def _ordered(self, items: Iterable[IndexedCandle], as_of: int, start_open: int | None = None) -> list[dict[str, Any]]:
        selected = [item for item in items if self._prefix(item, as_of) and (start_open is None or item.open_timestamp >= int(start_open))]
        selected.sort(key=lambda item: item.ordinal)
        return [item.bar for item in selected]

    def intersecting(self, zone_low: float, zone_high: float, as_of: int, start_open: int | None = None) -> list[dict[str, Any]]:
        """Return bars satisfying the reference reaction intersection predicate."""
        low_candidates = self._by_low[:bisect_right(self._lows, float(zone_high))]
        high_candidates = self._by_high[bisect_left(self._highs, float(zone_low)):]
        low_ordinals = {item.ordinal for item in low_candidates}
        matches = [item for item in high_candidates if item.ordinal in low_ordinals]
        return self._ordered(matches, as_of, start_open)

    def outside_close(self, zone_low: float, zone_high: float, as_of: int, start_open: int | None = None) -> list[dict[str, Any]]:
        """Return bars satisfying either exact role-flip close predicate."""
        below = self._by_close[:bisect_left(self._closes, float(zone_low))]
        above = self._by_close[bisect_right(self._closes, float(zone_high)):]
        return self._ordered((*below, *above), as_of, start_open)

    def prefix_records(self, as_of: int) -> list[dict[str, Any]]:
        return self._ordered(self._items, as_of)

    @property
    def source_count(self) -> int:
        return len(self._items)

    def candidate_count_intersection(self, zone_low: float, zone_high: float) -> int:
        low_candidates = self._by_low[:bisect_right(self._lows, float(zone_high))]
        high_candidates = self._by_high[bisect_left(self._highs, float(zone_low)):]
        return len({item.ordinal for item in low_candidates}.intersection(item.ordinal for item in high_candidates))
