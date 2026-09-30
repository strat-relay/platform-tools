"""Redis market-data cache: completed bars, symbol metadata, quotes.

The read bridge (22347) executes one command per EA poll, so its capacity is a command budget.
Completed bars never change, so they are fetched once and then only extended; symbol metadata
changes rarely; quotes are shared by every reader. One collector (collector.py) owns the bridge
reads; strategy runtimes and the Trade Manager read this cache.

Keys (provider symbols, e.g. EURUSDm):
    md:bars:<symbol>:<tf>    JSON list of completed bar rows, oldest first, verbatim from the bridge
    md:snapshot:<symbol>     JSON {fetched_at, symbol_info, quote, forming}: the latest snapshot's
                             non-bar parts (the runner's contract/quote come from here, so its
                             inputs are exactly one real bridge snapshot)
    md:meta:<symbol>         JSON {symbol_info, observed_at}
    md:quote:<symbol>        JSON {quote, observed_at}          (hot-quote refresher)
    md:state:<symbol>        JSON per-symbol collection state/health
    md:health                JSON collector health

The cache is never more authoritative than the broker: everything is timestamped, and readers fail
closed on missing or stale data instead of reading the bridge themselves.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

TIMEFRAMES = ("M5", "M15", "H1", "H4")
KEEP_BARS = 400            # >= the runner's 320-row window (319 completed) with headroom
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


class BarGap(Exception):
    """The incremental window does not connect to the cached series (or a cached bar was revised);
    a full refetch is required."""


def _row_time(row: dict[str, Any]) -> int:
    return int(row["time"])


def merge_completed(cached: list[dict[str, Any]], fresh: list[dict[str, Any]], *,
                    keep: int = KEEP_BARS) -> tuple[list[dict[str, Any]], int]:
    """Extend `cached` with the newer rows of `fresh` (both completed, oldest first).

    Parity rules: `fresh` must overlap the cache (its first row's time must be a cached time), and
    every overlapping row must equal the cached row exactly - otherwise the series has a gap or the
    broker revised history, and BarGap forces a full refetch. Returns (merged, rows_added)."""
    if not fresh:
        return cached, 0
    if not cached:
        raise BarGap("no cached series")
    by_time = {_row_time(r): r for r in cached}
    last = _row_time(cached[-1])
    if _row_time(fresh[0]) > last:
        raise BarGap("incremental window does not reach the cached series")
    for row in fresh:
        t = _row_time(row)
        if t <= last and by_time.get(t) != row:
            raise BarGap(f"cached bar at {t} differs from the broker's (missing or revised)")
    added = [r for r in fresh if _row_time(r) > last]
    merged = (cached + added)[-keep:]
    return merged, len(added)


def merge_by_time(*series: list[dict[str, Any]], keep: int = KEEP_BARS) -> list[dict[str, Any]]:
    """Merge completed bar series by candle identity, newest rows winning.

    Range recovery is deliberately idempotent: retrying a page or replaying a page after a
    process restart cannot duplicate candles.  Revisions are retained from the last supplied
    series because the collector has already validated the broker response for that page.
    """
    by_time: dict[int, dict[str, Any]] = {}
    for rows in series:
        for row in rows:
            by_time[_row_time(row)] = row
    return [by_time[t] for t in sorted(by_time)][-keep:]


def internal_missing_times(rows: list[dict[str, Any]], timeframe: str) -> list[int]:
    """Return bounded candle slots absent from a completed series.

    Only holes between the first and last returned candle are reported.  Session closures at the
    edges of a range are therefore not misclassified as a broker outage, while a missing interval
    surrounded by candles remains visible to recovery health and tests.
    """
    if len(rows) < 2:
        return []
    step = TIMEFRAME_SECONDS[timeframe]
    present = {_row_time(row) for row in rows}
    start, end = _row_time(rows[0]), _row_time(rows[-1])
    return [t for t in range(start + step, end, step) if t not in present]


def unexpected_missing_times(rows: list[dict[str, Any]], timeframe: str) -> list[int]:
    """Filter normal exchange/session closures from bounded continuity holes.

    The MT5 source legitimately omits closed-market candles (including the weekend and the
    recurring XAU maintenance window around 21:00 UTC).  Recovery must target a hole surrounded
    by live-session candles, not manufacture bars for a closed market.
    """
    missing = internal_missing_times(rows, timeframe)
    if not missing or timeframe not in TIMEFRAME_SECONDS:
        return missing
    unexpected = []
    for timestamp in missing:
        dt = datetime.fromtimestamp(timestamp, timezone.utc)
        if dt.weekday() in (5, 6):
            continue
        if dt.hour in (20, 21, 22):
            continue
        unexpected.append(timestamp)
    return unexpected


class MarketDataStore:
    def __init__(self, redis_client: Any):
        self.redis = redis_client

    def _get(self, key: str) -> Any:
        raw = self.redis.get(key)
        return None if raw is None else json.loads(raw)

    def _set(self, key: str, value: Any) -> None:
        self.redis.set(key, json.dumps(value, sort_keys=True, default=str))

    # bars
    def bars(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        return self._get(f"md:bars:{symbol}:{timeframe}") or []

    def set_bars(self, symbol: str, timeframe: str, rows: list[dict[str, Any]]) -> None:
        self._set(f"md:bars:{symbol}:{timeframe}", rows[-KEEP_BARS:])

    # snapshot remainder / metadata / quotes / state
    def snapshot(self, symbol: str) -> dict[str, Any] | None:
        return self._get(f"md:snapshot:{symbol}")

    def set_snapshot(self, symbol: str, value: dict[str, Any]) -> None:
        self._set(f"md:snapshot:{symbol}", value)

    def metadata(self, symbol: str) -> dict[str, Any] | None:
        return self._get(f"md:meta:{symbol}")

    def set_metadata(self, symbol: str, symbol_info: dict[str, Any], observed_at: float) -> None:
        self._set(f"md:meta:{symbol}", {"symbol_info": symbol_info, "observed_at": observed_at})

    def quote(self, symbol: str) -> dict[str, Any] | None:
        return self._get(f"md:quote:{symbol}")

    def set_quote(self, symbol: str, quote: dict[str, Any], observed_at: float) -> None:
        self._set(f"md:quote:{symbol}", {"quote": quote, "observed_at": observed_at})

    def state(self, symbol: str) -> dict[str, Any]:
        return self._get(f"md:state:{symbol}") or {}

    def set_state(self, symbol: str, value: dict[str, Any]) -> None:
        self._set(f"md:state:{symbol}", value)

    def health(self) -> dict[str, Any]:
        return self._get("md:health") or {}

    def set_health(self, value: dict[str, Any]) -> None:
        self._set("md:health", value)
