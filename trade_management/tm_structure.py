"""TM-STRUCTURE-1: structure-driven trade management (SL, TP and early exit).

Deterministic and pure like TM-NONE / TM-BREAKEVEN-TRAIL: inputs are the trade, the effective
stop/target, the observation quote and completed bars as of the observation (no I/O, no clock).
One action per observation, in this order (LONG shown; SHORT mirrors it):

  EXIT              stop already at breakeven or better, price in profit, and the last completed M15
                    bar closed below the latest confirmed M15 swing low (structure turned against).
  MOVE_TO_BREAKEVEN stop still below entry, and price >= `breakeven_r`, or >= `early_breakeven_r`
                    with a confirmed M5 swing low formed above entry after the trade opened.
                    New stop: entry + current spread (conservative: covers the close cost).
  MOVE_TARGET       (tighten) stop still below entry, price >= `tighten_min_r`, and the M15
                    structure turned against: take-profit pulled in to price + `tighten_offset_r`.
  MOVE_TARGET       (extend) at breakeven, >= `extend_progress` of the way to the target, and the
                    last M15 close broke above the latest confirmed M15 swing high: target moved to
                    the next confirmed H1 swing high beyond it (the next liquidity), capped at
                    `target_cap_r`; the stop is locked at >= min(`extend_lock_r`, half the open R).
  TRAIL_STOP        at breakeven: stop raised to the latest confirmed M5 swing low minus
                    `trail_buffer_r`, only if that tightens the stop and stays >= `min_trail_gap_r`
                    below price.
  HOLD              otherwise.

A stop only ever tightens; a target is only moved beyond price. Without bars the evaluator can
still move to breakeven at `breakeven_r` and otherwise holds (reason BARS_UNAVAILABLE).
A swing (2 bars each side) counts only once its right-hand bars have closed by the observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

EVALUATOR_ID = "tm-structure.v1"
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600}

REASON_TRADE_CLOSED = "TRADE_CLOSED"
REASON_MISSING_RISK_DISTANCE = "RISK_DISTANCE_UNAVAILABLE"
REASON_BARS_UNAVAILABLE = "BARS_UNAVAILABLE"
REASON_STRUCTURE_BREAK_AGAINST = "STRUCTURE_BREAK_AGAINST"
REASON_BREAKEVEN_R = "BREAKEVEN_TRIGGER_REACHED"
REASON_BREAKEVEN_STRUCTURE = "BREAKEVEN_STRUCTURE_CONFIRMED"
REASON_BREAKEVEN_NOT_YET = "BREAKEVEN_TRIGGER_NOT_REACHED"
REASON_TARGET_TIGHTENED = "TARGET_TIGHTENED_ON_WEAKNESS"
REASON_TARGET_EXTENDED = "TARGET_EXTENDED_TO_NEXT_LIQUIDITY"
REASON_TRAIL_ADVANCED = "TRAIL_STOP_TO_SWING"
REASON_NO_ACTION = "STRUCTURE_UNCHANGED"


@dataclass(frozen=True)
class StructurePolicy:
    breakeven_r: float = 1.0
    early_breakeven_r: float = 0.5
    trail_buffer_r: float = 0.1
    min_trail_gap_r: float = 0.25
    extend_progress: float = 0.8
    target_cap_r: float = 3.0
    extend_lock_r: float = 1.0
    tighten_min_r: float = 0.5
    tighten_offset_r: float = 0.25

    def __post_init__(self) -> None:
        if not 0 < self.early_breakeven_r <= self.breakeven_r:
            raise ValueError("0 < early_breakeven_r <= breakeven_r required")
        if self.trail_buffer_r < 0 or self.min_trail_gap_r <= 0 or self.tighten_offset_r <= 0:
            raise ValueError("trail_buffer_r >= 0, min_trail_gap_r > 0 and tighten_offset_r > 0 required")
        if not 0 < self.extend_progress <= 1 or self.target_cap_r <= 0 or self.extend_lock_r <= 0:
            raise ValueError("0 < extend_progress <= 1, target_cap_r > 0 and extend_lock_r > 0 required")
        if self.tighten_min_r <= 0:
            raise ValueError("tighten_min_r must be > 0")

    def as_bundle(self) -> tuple[tuple[str, float], ...]:
        return tuple((name, getattr(self, name)) for name in self.__dataclass_fields__)


def completed(bars: Sequence[Mapping[str, Any]] | None, timeframe: str, as_of: float) -> list[Mapping[str, Any]]:
    step = TF_SECONDS[timeframe]
    return sorted((b for b in (bars or ()) if float(b["time"]) + step <= as_of), key=lambda b: float(b["time"]))


def confirmed_swings(bars: Sequence[Mapping[str, Any]], side: str, lookback: int = 2) -> list[tuple[float, float]]:
    """(bar time, price) of strict swing highs/lows among completed bars; the last `lookback` bars
    cannot be swings yet (their right-hand side has not closed)."""
    out = []
    for i in range(lookback, len(bars) - lookback):
        value = float(bars[i][side])
        others = [float(bars[j][side]) for j in range(i - lookback, i + lookback + 1) if j != i]
        if (side == "high" and value > max(others)) or (side == "low" and value < min(others)):
            out.append((float(bars[i]["time"]), value))
    return out


class TmStructureEvaluator:
    evaluator_id = EVALUATOR_ID

    def evaluate(self, *, direction: str, entry: float, risk_distance: float | None, current_stop: float,
                 current_target: float | None, bid: float, ask: float, trade_state: str, entry_time: float,
                 as_of: float, bars: Mapping[str, Sequence[Mapping[str, Any]]] | None,
                 policy: StructurePolicy) -> tuple[str, tuple[str, ...], dict]:
        if trade_state != "OPEN":
            return "HOLD", (REASON_TRADE_CLOSED,), {}
        if not risk_distance or risk_distance <= 0:
            return "HOLD", (REASON_MISSING_RISK_DISTANCE,), {}
        long = direction == "LONG"
        s = 1.0 if long else -1.0
        mark = bid if long else ask
        spread = max(0.0, ask - bid)
        r = s * (mark - entry) / risk_distance
        at_breakeven = s * (current_stop - entry) >= 0
        base = {"r_multiple": r}

        m5 = completed((bars or {}).get("M5"), "M5", as_of)
        m15 = completed((bars or {}).get("M15"), "M15", as_of)
        h1 = completed((bars or {}).get("H1"), "H1", as_of)
        if not m5 or not m15:
            if not at_breakeven and r >= policy.breakeven_r:
                return "MOVE_TO_BREAKEVEN", (REASON_BREAKEVEN_R, REASON_BARS_UNAVAILABLE), {**base, "new_stop": entry + s * spread}
            return "HOLD", (REASON_BARS_UNAVAILABLE,), base

        protective, opposing = ("low", "high") if long else ("high", "low")
        m15_protective = confirmed_swings(m15, protective)
        m15_opposing = confirmed_swings(m15, opposing)
        last_close = float(m15[-1]["close"])
        against_level = m15_protective[-1][1] if m15_protective else None
        broke_against = against_level is not None and s * (last_close - against_level) < 0
        evidence = {"m15_close": last_close, "m15_protective_swing": against_level}

        # 1. Structure turned against a protected trade in profit: take it.
        if at_breakeven and r > 0 and broke_against:
            return "EXIT", (REASON_STRUCTURE_BREAK_AGAINST,), {**base, **evidence}

        if not at_breakeven:
            # 2. Protect first: breakeven at breakeven_r, or earlier once structure confirms.
            if r >= policy.breakeven_r:
                return "MOVE_TO_BREAKEVEN", (REASON_BREAKEVEN_R,), {**base, "new_stop": entry + s * spread}
            fresh_swings = [p for t, p in confirmed_swings(m5, protective) if t >= entry_time and s * (p - entry) > 0]
            if r >= policy.early_breakeven_r and fresh_swings:
                return "MOVE_TO_BREAKEVEN", (REASON_BREAKEVEN_STRUCTURE,), {
                    **base, "new_stop": entry + s * spread, "m5_swing": fresh_swings[-1]}
            # 3. Weakness before breakeven: bank what is there by pulling the target in.
            if r >= policy.tighten_min_r and broke_against and current_target:
                new_target = mark + s * policy.tighten_offset_r * risk_distance
                if s * (new_target - current_target) < 0:
                    return "MOVE_TARGET", (REASON_TARGET_TIGHTENED,), {**base, **evidence, "new_target": new_target}
            return "HOLD", (REASON_BREAKEVEN_NOT_YET,), base

        # 4. Strength near the target: extend it to the next liquidity, locking profit.
        if current_target and s * (current_target - entry) > 0:
            progress = s * (mark - entry) / (s * (current_target - entry))
            opposing_level = m15_opposing[-1][1] if m15_opposing else None
            broke_with = opposing_level is not None and s * (last_close - opposing_level) > 0
            if progress >= policy.extend_progress and broke_with:
                beyond = [p for _, p in confirmed_swings(h1, opposing) if s * (p - current_target) > 0]
                cap = entry + s * policy.target_cap_r * risk_distance
                if beyond:
                    candidate = min(beyond) if long else max(beyond)
                    new_target = min(candidate, cap) if long else max(candidate, cap)
                    lock = entry + s * min(policy.extend_lock_r, 0.5 * r) * risk_distance
                    new_stop = max(current_stop, lock) if long else min(current_stop, lock)
                    if s * (new_target - current_target) > 0:
                        return "MOVE_TARGET", (REASON_TARGET_EXTENDED,), {
                            **base, **evidence, "new_target": new_target, "new_stop": new_stop,
                            "m15_opposing_swing": opposing_level, "h1_liquidity": candidate}

        # 5. Trail behind the latest confirmed M5 swing.
        m5_protective = confirmed_swings(m5, protective)
        if m5_protective:
            swing = m5_protective[-1][1]
            candidate = swing - s * policy.trail_buffer_r * risk_distance
            if s * (candidate - current_stop) > 0 and s * (mark - candidate) >= policy.min_trail_gap_r * risk_distance:
                return "TRAIL_STOP", (REASON_TRAIL_ADVANCED,), {**base, "new_stop": candidate, "m5_swing": swing}
        return "HOLD", (REASON_NO_ACTION,), base
