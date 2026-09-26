"""TM-BREAKEVEN-TRAIL-1: the first real (non-HOLD-only) TradeManagerVersion evaluator.

Two actions only, matching the ACTIONS vocabulary already frozen in versions.py:
  MOVE_TO_BREAKEVEN - once the trade has moved `breakeven_trigger_r` in its favor and the stop
                       has not already been moved to breakeven, move it to entry.
  TRAIL_STOP        - once the trade has moved `trail_trigger_r` in its favor (only ever
                       evaluated after breakeven has already been reached), advance the stop to
                       `trail_distance_r` behind the current mark, but only when that is a strict
                       improvement over the current stop - a trailing stop only ever tightens.

No randomness, no I/O, no provider call - deterministic by construction, matching TM-NONE's own
stated design rule (A6 11 section 5). This module never decides whether the resulting decision is
published: that remains publication_gate.py's job, called by decision_engine.py exactly the way
tm_none.py already calls it, with the same SHADOW_ONLY default - this evaluator can widen what
`action` a decision holds, but never touches whether anything gets published or reaches a broker.
"""
from __future__ import annotations

from dataclasses import dataclass

REASON_TRADE_CLOSED = "TRADE_CLOSED"
REASON_BREAKEVEN_NOT_YET = "BREAKEVEN_TRIGGER_NOT_REACHED"
REASON_BREAKEVEN_TRIGGERED = "BREAKEVEN_TRIGGER_REACHED"
REASON_TRAIL_NOT_YET = "TRAIL_TRIGGER_NOT_REACHED"
REASON_TRAIL_ADVANCED = "TRAIL_STOP_ADVANCED"
REASON_TRAIL_NO_IMPROVEMENT = "TRAIL_CANDIDATE_NOT_AN_IMPROVEMENT"
REASON_MISSING_RISK_DISTANCE = "RISK_DISTANCE_UNAVAILABLE"

EVALUATOR_ID = "tm-breakeven-trail.v1"


@dataclass(frozen=True)
class BreakevenTrailPolicy:
    """The policy_bundle parameters a bound TmVersionManifest carries - see
    versions.py::tm_breakeven_trail_manifest(). All three are expressed in R (risk multiples),
    matching TM-NONE's own `r_multiple_definition` convention exactly, so a decision's
    `parameters.r_multiple` means the same thing regardless of which evaluator produced it."""
    breakeven_trigger_r: float
    trail_trigger_r: float
    trail_distance_r: float

    def __post_init__(self) -> None:
        if self.breakeven_trigger_r <= 0:
            raise ValueError("breakeven_trigger_r must be > 0")
        if self.trail_trigger_r < self.breakeven_trigger_r:
            raise ValueError("trail_trigger_r must be >= breakeven_trigger_r")
        if self.trail_distance_r <= 0:
            raise ValueError("trail_distance_r must be > 0")


class TmBreakevenTrailEvaluator:
    evaluator_id = EVALUATOR_ID

    def evaluate(self, *, direction: str, entry: float, initial_stop: float, risk_distance: float | None,
                current_stop: float, mark_price: float, trade_state: str,
                policy: BreakevenTrailPolicy) -> tuple[str, tuple[str, ...], dict]:
        if trade_state != "OPEN":
            return "HOLD", (REASON_TRADE_CLOSED,), {}
        if not risk_distance or risk_distance <= 0:
            # Fail closed on this specific trade's evaluation only - never guess a distance from
            # entry/initial_stop when the canonical risk_distance is missing/invalid; the next
            # observation retries.
            return "HOLD", (REASON_MISSING_RISK_DISTANCE,), {}

        sign = 1.0 if direction == "LONG" else -1.0
        r_multiple = sign * (mark_price - entry) / risk_distance
        at_breakeven = sign * (current_stop - entry) >= 0

        if not at_breakeven:
            if r_multiple >= policy.breakeven_trigger_r:
                return ("MOVE_TO_BREAKEVEN", (REASON_BREAKEVEN_TRIGGERED,),
                        {"new_stop": entry, "r_multiple": r_multiple})
            return "HOLD", (REASON_BREAKEVEN_NOT_YET,), {"r_multiple": r_multiple}

        if r_multiple < policy.trail_trigger_r:
            return "HOLD", (REASON_TRAIL_NOT_YET,), {"r_multiple": r_multiple}

        candidate_stop = mark_price - sign * policy.trail_distance_r * risk_distance
        improves = sign * (candidate_stop - current_stop) > 0
        if not improves:
            return "HOLD", (REASON_TRAIL_NO_IMPROVEMENT,), {"r_multiple": r_multiple}
        return ("TRAIL_STOP", (REASON_TRAIL_ADVANCED,),
                {"new_stop": candidate_stop, "r_multiple": r_multiple})
