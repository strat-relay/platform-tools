"""Central, read-only pre-broker tradeability gate."""
from __future__ import annotations

import math
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradeabilityResult:
    status: str
    rejection_reason: str | None
    signal_entry: float
    expected_execution_entry: float
    original_stop_distance: float
    original_target_distance: float
    original_rr: float | None
    remaining_target_distance: float
    effective_stop_distance: float
    effective_target_distance: float
    effective_rr: float | None
    bid: float
    ask: float
    spread: float
    spread_as_percent_of_remaining_target: float | None
    spread_cost_R: float | None
    point: float | None
    tick_size: float | None
    broker_min_stop_distance: float | None
    trade_stops_level: float | None
    trade_freeze_level: float | None
    stop_tick_aligned: bool
    target_tick_aligned: bool
    target_already_consumed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _aligned(price: float, tick: float | None) -> bool:
    if not tick or tick <= 0:
        return False
    return math.isclose(price / tick, round(price / tick), rel_tol=0, abs_tol=1e-8)


def load_policy(root: Path | None = None) -> dict[str, Any]:
    path = (root or Path(__file__).resolve().parent) / "config" / "tradeability_policy.json"
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(signal: dict[str, Any], quote: dict[str, Any], metadata: dict[str, Any],
             *, policy_min_rr: float | None = None) -> TradeabilityResult:
    direction = str(signal.get("direction", "")).upper()
    long = direction in {"LONG", "BUY"}
    entry = float(signal["entry_price"])
    stop = float(signal["stop_price"])
    target = float(signal["target_price"])
    bid, ask = float(quote["bid"]), float(quote["ask"])
    expected = ask if long else bid
    spread = max(0.0, ask - bid)
    point = metadata.get("point", metadata.get("tick_size"))
    tick = metadata.get("tick_size", point)
    point = float(point) if point is not None else None
    tick = float(tick) if tick is not None else None
    stops_level = float(metadata.get("stops_level") or 0) if metadata.get("stops_level") is not None else None
    freeze_level = float(metadata.get("freeze_level") or 0) if metadata.get("freeze_level") is not None else None
    levels = [x for x in (stops_level, freeze_level) if x is not None]
    broker_min = max(levels) * point if levels and point is not None else None
    original_stop = abs(entry - stop)
    original_target = abs(target - entry)
    original_rr = original_target / original_stop if original_stop > 0 else None
    effective_stop = (expected - stop) if long else (stop - expected)
    effective_target = (target - expected) if long else (expected - target)
    consumed = (ask >= target) if long else (bid <= target)
    stop_aligned, target_aligned = _aligned(stop, tick), _aligned(target, tick)
    percent = spread / effective_target * 100 if effective_target > 0 else None
    spread_r = spread / effective_stop if effective_stop > 0 else None
    effective_rr = effective_target / effective_stop if effective_stop > 0 and effective_target > 0 else None
    reason = None
    status = "TRADEABLE"
    if (long and stop >= expected) or ((not long) and stop <= expected):
        status, reason = "WRONG_SIDE_OF_MARKET", "STOP_WRONG_SIDE_OF_MARKET"
    # First reject a structurally invalid target relative to the strategy's
    # proposed entry.  A valid target that the live quote has crossed is
    # reported separately as TARGET_ALREADY_CONSUMED.
    elif (long and target <= entry) or ((not long) and target >= entry):
        status, reason = "WRONG_SIDE_OF_MARKET", "TARGET_WRONG_SIDE_OF_MARKET"
    elif consumed:
        status, reason = "TARGET_ALREADY_CONSUMED", "TARGET_ALREADY_CONSUMED"
    elif (long and target <= expected) or ((not long) and target >= expected):
        status, reason = "WRONG_SIDE_OF_MARKET", "TARGET_WRONG_SIDE_OF_MARKET"
    elif broker_min is not None and effective_stop < broker_min:
        status, reason = "STOP_TOO_CLOSE", "BROKER_STOP_TOO_CLOSE"
    elif broker_min is not None and effective_target < broker_min:
        status, reason = "TARGET_TOO_CLOSE", "BROKER_TARGET_TOO_CLOSE"
    elif not stop_aligned or not target_aligned:
        status, reason = "INVALID_TICK_ALIGNMENT", "INVALID_TICK_ALIGNMENT"
    elif effective_target <= spread:
        status, reason = "SPREAD_DOMINATES_TARGET", "SPREAD_DOMINATES_TARGET"
    elif policy_min_rr is not None and (effective_rr is None or effective_rr < policy_min_rr):
        status, reason = "RR_BELOW_POLICY", "EFFECTIVE_RR_BELOW_MINIMUM"
    return TradeabilityResult(status, reason, entry, expected, original_stop, original_target,
        original_rr, max(0.0, effective_target), effective_stop, max(0.0, effective_target),
        effective_rr, bid, ask, spread, percent, spread_r, point, tick, broker_min,
        stops_level, freeze_level, stop_aligned, target_aligned, consumed)
