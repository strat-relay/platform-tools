from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from bisect import bisect_right
from typing import Any


TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240}
FORMING_SOURCE = {"M5": "M1", "M15": "M5", "H1": "M5", "H4": "M15"}


def _ts(value: Any) -> int:
    return int(value)


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def timeframe_seconds(timeframe: str) -> int:
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return TIMEFRAME_MINUTES[timeframe] * 60


def normalize_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(x) for x in bars), key=lambda x: _ts(x["time"]))


def bar_end(bar: dict[str, Any], timeframe: str) -> int:
    return _ts(bar["time"]) + timeframe_seconds(timeframe)


def floor_timestamp(ts: int, timeframe: str) -> int:
    seconds = timeframe_seconds(timeframe)
    return int(ts) - int(ts) % seconds


def aggregate_bars(bars: list[dict[str, Any]], timeframe: str, start: int, end: int) -> dict[str, Any] | None:
    selected = [x for x in bars if start <= _ts(x["time"]) < end]
    if not selected:
        return None
    return {
        "time": start,
        "open": float(selected[0]["open"]),
        "high": max(float(x["high"]) for x in selected),
        "low": min(float(x["low"]) for x in selected),
        "close": float(selected[-1]["close"]),
        "spread": int(selected[-1].get("spread", 0)),
        "tick_volume": sum(int(x.get("tick_volume", 0)) for x in selected),
        "forming": True,
        "source_timeframe": timeframe,
        "source_bar_count": len(selected),
        "source_last_timestamp": _ts(selected[-1]["time"]),
    }


@dataclass(frozen=True)
class CausalReplay:
    """Read-only timestamped view over bars.

    A bar is completed only when its close time is at or before ``as_of``.
    Forming higher-timeframe bars are rebuilt from lower-timeframe bars whose
    open timestamps are strictly before ``as_of``. Future OHLC values are never
    read by any view method.
    """

    bars_by_timeframe: dict[str, list[dict[str, Any]]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bars_by_timeframe",
            {tf: normalize_bars(bars) for tf, bars in self.bars_by_timeframe.items()},
        )

    def history(self, timeframe: str, as_of: int) -> list[dict[str, Any]]:
        bars = self.bars_by_timeframe.get(timeframe, [])
        cutoff = int(as_of) - timeframe_seconds(timeframe)
        times = [int(x["time"]) for x in bars]
        end = bisect_right(times, cutoff)
        return [dict(x) for x in bars[:end]]

    def forming(self, timeframe: str, as_of: int) -> dict[str, Any] | None:
        source_tf = FORMING_SOURCE.get(timeframe)
        source = self.bars_by_timeframe.get(source_tf or "", [])
        if not source:
            return None
        start = floor_timestamp(int(as_of), timeframe)
        return aggregate_bars(
            [x for x in source if _ts(x["time"]) < int(as_of)],
            timeframe,
            start,
            int(as_of),
        )

    def view(self, timeframe: str, as_of: int) -> dict[str, Any]:
        completed = self.history(timeframe, as_of)
        forming = self.forming(timeframe, as_of)
        return {
            "timeframe": timeframe,
            "as_of": iso(as_of),
            "as_of_timestamp": int(as_of),
            "completed": completed[-1] if completed else None,
            "completed_bars": completed,
            "forming": forming,
            "provenance": {
                "as_of_timestamp": int(as_of),
                "completed_bar_rule": "bar open + timeframe duration <= as_of",
                "forming_bar_rule": "lower-timeframe bars with open timestamp < as_of",
                "forming_source_timeframe": FORMING_SOURCE.get(timeframe),
                "future_bars_excluded": True,
            },
        }

    def synchronized_context(self, as_of: int, timeframes: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Return a causal context for caller-supplied timeframes."""
        selected = timeframes or ("M5", "M15", "H1", "H4")
        return {tf: self.view(tf, as_of) for tf in selected}

    def window(self, as_of: int, limits: dict[str, int] | None = None) -> "CausalReplay":
        limits = limits or {"M1": 5000, "M5": 2500, "M15": 1000, "H1": 400, "H4": 100}
        selected = {}
        for tf in self.bars_by_timeframe:
            history = self.history(tf, as_of)
            selected[tf] = history[-limits.get(tf, len(history)):]
        return CausalReplay(selected)
