"""Pure decision-logic tests for trade_management/tm_breakeven_trail.py - no database, no
transport. Mirrors the R-multiple/price-semantics conventions TM-NONE-1's own manifest already
documents (bid for LONG, ask for SHORT; r_multiple = (price-entry)/risk_distance signed by
direction) so a LONG position's numbers below double as the SHORT case's mirror image.
"""
from __future__ import annotations

import unittest

from trade_management.tm_breakeven_trail import (
    REASON_BREAKEVEN_NOT_YET,
    REASON_BREAKEVEN_TRIGGERED,
    REASON_MISSING_RISK_DISTANCE,
    REASON_TRADE_CLOSED,
    REASON_TRAIL_ADVANCED,
    REASON_TRAIL_NOT_YET,
    REASON_TRAIL_NO_IMPROVEMENT,
    BreakevenTrailPolicy,
    TmBreakevenTrailEvaluator,
)

POLICY = BreakevenTrailPolicy(breakeven_trigger_r=1.0, trail_trigger_r=1.5, trail_distance_r=0.5)


def evaluate(**overrides):
    defaults = dict(direction="LONG", entry=100.0, initial_stop=99.0, risk_distance=1.0,
                    current_stop=99.0, mark_price=100.0, trade_state="OPEN", policy=POLICY)
    defaults.update(overrides)
    return TmBreakevenTrailEvaluator().evaluate(**defaults)


class PolicyValidationTests(unittest.TestCase):
    def test_breakeven_trigger_must_be_positive(self):
        with self.assertRaises(ValueError):
            BreakevenTrailPolicy(breakeven_trigger_r=0, trail_trigger_r=1.0, trail_distance_r=0.5)

    def test_trail_trigger_must_be_at_least_breakeven_trigger(self):
        with self.assertRaises(ValueError):
            BreakevenTrailPolicy(breakeven_trigger_r=1.0, trail_trigger_r=0.5, trail_distance_r=0.5)

    def test_trail_distance_must_be_positive(self):
        with self.assertRaises(ValueError):
            BreakevenTrailPolicy(breakeven_trigger_r=1.0, trail_trigger_r=1.5, trail_distance_r=0)


class ClosedOrMissingDataTests(unittest.TestCase):
    def test_a_closed_trade_always_holds_regardless_of_price(self):
        action, reasons, params = evaluate(trade_state="CLOSED", mark_price=150.0)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_TRADE_CLOSED,))
        self.assertEqual(params, {})

    def test_missing_risk_distance_fails_closed_to_hold(self):
        action, reasons, params = evaluate(risk_distance=None, mark_price=150.0)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_MISSING_RISK_DISTANCE,))

    def test_zero_risk_distance_fails_closed_to_hold(self):
        action, reasons, _ = evaluate(risk_distance=0, mark_price=150.0)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_MISSING_RISK_DISTANCE,))


class BreakevenTests(unittest.TestCase):
    def test_below_breakeven_trigger_holds_and_reports_the_current_r_multiple(self):
        # entry=100, stop=99 (risk_distance=1), mark=100.5 -> r=0.5, below the 1.0 trigger.
        action, reasons, params = evaluate(mark_price=100.5)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_BREAKEVEN_NOT_YET,))
        self.assertEqual(params["r_multiple"], 0.5)

    def test_at_the_breakeven_trigger_moves_to_breakeven(self):
        action, reasons, params = evaluate(mark_price=101.0)  # r = 1.0, exactly at trigger
        self.assertEqual(action, "MOVE_TO_BREAKEVEN")
        self.assertEqual(reasons, (REASON_BREAKEVEN_TRIGGERED,))
        self.assertEqual(params["new_stop"], 100.0)  # entry
        self.assertEqual(params["r_multiple"], 1.0)

    def test_short_direction_mirrors_long(self):
        # SHORT entry=100, stop=101 (risk_distance=1); mark=99.0 -> favorable by 1R.
        action, reasons, params = evaluate(direction="SHORT", entry=100.0, initial_stop=101.0,
                                           current_stop=101.0, mark_price=99.0)
        self.assertEqual(action, "MOVE_TO_BREAKEVEN")
        self.assertEqual(params["new_stop"], 100.0)
        self.assertEqual(params["r_multiple"], 1.0)

    def test_already_at_breakeven_does_not_move_to_breakeven_again(self):
        # current_stop already at entry (100) - even well past the trigger, this must fall
        # through to the trailing branch, not re-fire MOVE_TO_BREAKEVEN.
        action, _, _ = evaluate(current_stop=100.0, mark_price=101.0)
        self.assertNotEqual(action, "MOVE_TO_BREAKEVEN")


class TrailingTests(unittest.TestCase):
    def test_at_breakeven_but_below_trail_trigger_holds(self):
        # r=1.2, between breakeven_trigger (1.0) and trail_trigger (1.5).
        action, reasons, params = evaluate(current_stop=100.0, mark_price=101.2)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_TRAIL_NOT_YET,))
        self.assertAlmostEqual(params["r_multiple"], 1.2)

    def test_at_trail_trigger_advances_the_stop(self):
        # r=1.5 exactly at trail_trigger; trail_distance_r=0.5 -> new_stop = mark - 0.5*risk = 101.0.
        action, reasons, params = evaluate(current_stop=100.0, mark_price=101.5)
        self.assertEqual(action, "TRAIL_STOP")
        self.assertEqual(reasons, (REASON_TRAIL_ADVANCED,))
        self.assertEqual(params["new_stop"], 101.0)
        self.assertEqual(params["r_multiple"], 1.5)

    def test_a_trail_candidate_that_does_not_improve_the_stop_holds(self):
        # current_stop already at 101.0 (from a prior trail); mark only ticked up to 101.5 again -
        # the candidate (101.0) is not a strict improvement over the current stop.
        action, reasons, params = evaluate(current_stop=101.0, mark_price=101.5)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, (REASON_TRAIL_NO_IMPROVEMENT,))

    def test_trail_only_ever_tightens_never_widens(self):
        # A large favorable move should never produce a stop worse than the current one.
        action, reasons, params = evaluate(current_stop=103.0, mark_price=104.0)
        if action == "TRAIL_STOP":
            self.assertGreaterEqual(params["new_stop"], 103.0)
        else:
            self.assertEqual(reasons, (REASON_TRAIL_NO_IMPROVEMENT,))

    def test_short_trailing_mirrors_long(self):
        # SHORT: entry=100, already at breakeven (current_stop=100), mark=98.5 -> r=1.5.
        action, reasons, params = evaluate(direction="SHORT", entry=100.0, initial_stop=101.0,
                                           current_stop=100.0, mark_price=98.5)
        self.assertEqual(action, "TRAIL_STOP")
        self.assertEqual(params["new_stop"], 99.0)  # mark + 0.5*risk_distance
        self.assertEqual(params["r_multiple"], 1.5)


if __name__ == "__main__":
    unittest.main()
