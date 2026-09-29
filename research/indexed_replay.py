"""Full-prefix causal replay view backed by exact historical indexes."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any

from context_structure_retrace.data import CausalReplay, aggregate_bars, timeframe_seconds
from research.exact_history_index import ExactHistoricalCandleIndex


class IndexedCausalReplay(CausalReplay):
    """Avoid repeated prefix slicing while preserving CausalReplay semantics."""

    def __init__(self, bars_by_timeframe: dict[str, list[dict[str, Any]]], *, indexes: dict[str, ExactHistoricalCandleIndex] | None = None, window_limits: dict[str, int] | None = None) -> None:
        super().__init__(bars_by_timeframe)
        object.__setattr__(self, "_times", {tf: tuple(int(bar["time"]) for bar in bars) for tf, bars in self.bars_by_timeframe.items()})
        object.__setattr__(self, "_indexes", indexes or {tf: ExactHistoricalCandleIndex(bars, tf) for tf, bars in self.bars_by_timeframe.items()})
        object.__setattr__(self, "_window_limits", window_limits or {})

    def history(self, timeframe: str, as_of: int) -> list[dict[str, Any]]:
        bars = self.bars_by_timeframe.get(timeframe, [])
        cutoff = int(as_of) - timeframe_seconds(timeframe)
        end = bisect_right(self._times.get(timeframe, ()), cutoff)
        start = max(0, end - int(self._window_limits.get(timeframe, end)))
        return list(bars[start:end])

    def window(self, as_of: int, limits: dict[str, int] | None = None) -> "IndexedCausalReplay":
        # Feature functions use history()/forming(), which enforce the causal
        # cutoff. Returning the immutable full-prefix view avoids copying every
        # historical bar for every M15 decision.
        return IndexedCausalReplay(self.bars_by_timeframe, indexes=self._indexes, window_limits=limits or self._window_limits)

    def forming(self, timeframe: str, as_of: int) -> dict[str, Any] | None:
        source_tf = {"M5": "M1", "M15": "M5", "H1": "M5", "H4": "M15"}.get(timeframe)
        source = self.bars_by_timeframe.get(source_tf or "", [])
        if not source:
            return None
        start = int(as_of) - int(as_of) % timeframe_seconds(timeframe)
        times = self._times.get(source_tf or "", ())
        end = bisect_left(times, int(as_of))
        first = bisect_left(times, start, 0, end)
        limit = int(self._window_limits.get(source_tf or "", end - first))
        first = max(first, end - limit)
        return aggregate_bars(source[first:end], timeframe, start, int(as_of))

    def historical_index(self, timeframe: str) -> ExactHistoricalCandleIndex:
        return self._indexes[timeframe]
