"""Causal and broker-authoritative outcome attribution primitives.

This module contains no database or broker I/O.  It is deliberately small so the
same rules can be used by the forward runner, the audit, and live reconciliation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def as_epoch(value: Any) -> float:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def target_distance(entry: Any, target: Any) -> float:
    """Return the unsigned distance serialized in a signal contract."""
    return abs(float(target) - float(entry))


def validate_temporal_order(*, signal_emitted_at: Any | None = None,
                           broker_fill_time: Any | None = None,
                           broker_exit_time: Any | None = None,
                           outcome_exit_time: Any | None = None,
                           historical_replay: bool = False) -> str | None:
    """Return a stable rejection code, or ``None`` when ordering is valid."""
    if broker_fill_time is not None and broker_exit_time is not None:
        if as_epoch(broker_exit_time) < as_epoch(broker_fill_time):
            return "INVALID_OUTCOME_TEMPORAL_ORDER"
    if not historical_replay and outcome_exit_time is not None:
        if signal_emitted_at is not None and as_epoch(outcome_exit_time) < as_epoch(signal_emitted_at):
            return "INVALID_OUTCOME_TEMPORAL_ORDER"
        if broker_fill_time is not None and as_epoch(outcome_exit_time) < as_epoch(broker_fill_time):
            return "INVALID_OUTCOME_TEMPORAL_ORDER"
    return None


def _touches(direction: str, bar: dict[str, Any], stop: float, target: float) -> tuple[bool, bool]:
    low, high = float(bar["low"]), float(bar["high"])
    if direction == "LONG":
        return low <= stop, high >= target
    return high >= stop, low <= target


def resolve_intrabar(direction: str, bars: Iterable[dict[str, Any]], *, stop: float,
                     target: float, entry_time: Any) -> dict[str, Any]:
    """Resolve a candle collision using ordered finer-grained bars.

    The first qualifying M1/tick bar wins.  If a single finer bar touches both
    levels, ordering is still unknown and remains ambiguous.
    """
    eligible = as_epoch(entry_time)
    for bar in sorted(bars, key=lambda item: as_epoch(item["time"])):
        if as_epoch(bar["time"]) < eligible:
            continue
        hit_stop, hit_target = _touches(direction, bar, stop, target)
        if hit_stop and hit_target:
            return {"status": "AMBIGUOUS_INTRABAR", "realized_r": 0.0,
                    "exit_timestamp": as_epoch(bar["time"]), "reason": "UNKNOWN_ORDERING"}
        if hit_stop:
            return {"status": "STOPPED", "realized_r": -1.0,
                    "exit_timestamp": as_epoch(bar["time"]), "reason": "STOP_HIT"}
        if hit_target:
            return {"status": "TARGET_HIT", "exit_timestamp": as_epoch(bar["time"]),
                    "reason": "TARGET_HIT"}
    return {"status": "OPEN", "realized_r": None, "exit_timestamp": None,
            "reason": "NO_LEVEL_TOUCHED"}


def resolve_candle(direction: str, bar: dict[str, Any], *, stop: float, target: float,
                  entry_time: Any, finer_bars: Iterable[dict[str, Any]] | None = None,
                  target_r: float | None = None) -> dict[str, Any]:
    """Resolve one source candle without admitting pre-entry movement."""
    start = as_epoch(bar["time"])
    end = start + float(bar.get("duration_seconds", 300))
    eligible = as_epoch(entry_time)
    if end <= eligible:
        return {"status": "OPEN", "realized_r": None, "exit_timestamp": None,
                "reason": "CANDLE_BEFORE_ENTRY"}
    hit_stop, hit_target = _touches(direction, bar, stop, target)
    if start < eligible < end:
        if finer_bars is not None:
            return resolve_intrabar(direction, finer_bars, stop=stop, target=target,
                                    entry_time=entry_time)
        # A full OHLC bar cannot prove that a touch happened after entry.
        return {"status": "OPEN", "realized_r": None, "exit_timestamp": None,
                "reason": "PRE_ENTRY_CANDLE_EXCLUDED"}
    if hit_stop and hit_target:
        return {"status": "AMBIGUOUS_INTRABAR", "realized_r": 0.0,
                "exit_timestamp": start, "reason": "UNKNOWN_ORDERING"}
    if hit_stop:
        return {"status": "STOPPED", "realized_r": -1.0,
                "exit_timestamp": start, "reason": "STOP_HIT"}
    if hit_target:
        return {"status": "TARGET_HIT", "realized_r": target_r,
                "exit_timestamp": start, "reason": "TARGET_HIT"}
    return {"status": "OPEN", "realized_r": None, "exit_timestamp": None,
            "reason": "NO_LEVEL_TOUCHED"}


def broker_authoritative(*, strategy_outcome: str | None, strategy_realized_r: float | None,
                         broker_outcome: str | None, broker_realized_r: float | None,
                         broker_exit_time: Any | None = None,
                         signal_emitted_at: Any | None = None,
                         broker_fill_time: Any | None = None) -> dict[str, Any]:
    """Build the display outcome while retaining theoretical strategy evidence."""
    invalid = validate_temporal_order(signal_emitted_at=signal_emitted_at,
                                      broker_fill_time=broker_fill_time,
                                      broker_exit_time=broker_exit_time,
                                      outcome_exit_time=broker_exit_time)
    if invalid:
        return {"status": "INVALIDATED", "realized_r": None,
                "reason": invalid, "strategy_outcome": strategy_outcome,
                "strategy_realized_r": strategy_realized_r}
    if broker_outcome is None:
        return {"status": strategy_outcome, "realized_r": strategy_realized_r,
                "reason": "BROKER_CLOSE_TRUTH_PENDING",
                "strategy_outcome": strategy_outcome,
                "strategy_realized_r": strategy_realized_r}
    return {"status": broker_outcome, "realized_r": broker_realized_r,
            "reason": "BROKER_AUTHORITATIVE", "strategy_outcome": strategy_outcome,
            "strategy_realized_r": strategy_realized_r}
