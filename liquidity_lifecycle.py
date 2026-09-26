"""Liquidity-owned entry lifecycle and canonical outcome classification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class LiquidityOutcome:
    status: str
    realized_r: float | None
    exit_timestamp: str


def _iso_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def settle_filled_entry(*, direction: str, entry: float, stop: float, target: float,
                        fill_timestamp: Any, bars: list[dict[str, Any]], max_hold_minutes: int) -> LiquidityOutcome | None:
    """Apply the frozen paper lifecycle ordering without broker assumptions.

    At the same completed bar, stop wins over target.  At/after the bounded hold,
    TIME_EXIT wins before price-hit classification, matching the authoritative
    forward implementation.  No result means the entry remains OPEN.
    """
    fill_epoch = _iso_epoch(fill_timestamp)
    long = direction == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("entry/stop must define positive risk")
    for bar in bars:
        close_epoch = _iso_epoch(bar["time"]) + 300
        if close_epoch < fill_epoch:
            continue
        elapsed = close_epoch - fill_epoch
        hit_stop = float(bar["low"]) <= stop if long else float(bar["high"]) >= stop
        hit_target = float(bar["high"]) >= target if long else float(bar["low"]) <= target
        exit_at = datetime.fromtimestamp(close_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        if elapsed >= max_hold_minutes * 60:
            exit_price = float(bar["close"])
            realized = (exit_price - entry) / risk if long else (entry - exit_price) / risk
            return LiquidityOutcome("TIME_EXIT", realized, exit_at)
        if hit_stop:
            return LiquidityOutcome("STOPPED", -1.0, exit_at)
        if hit_target:
            return LiquidityOutcome("TARGET_HIT", (target - entry) / risk if long else (entry - target) / risk, exit_at)
    return None
