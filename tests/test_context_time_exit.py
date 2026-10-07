from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from trade_management.tm_time_exit import evaluate_time_exit


UTC = timezone.utc
FILL = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class ContextTimeExitTests(unittest.TestCase):
    def test_holds_before_captured_deadline(self):
        action, reasons, parameters = evaluate_time_exit(
            trade_state="OPEN", time_exit_at=FILL + timedelta(minutes=15), as_of=FILL + timedelta(minutes=14, seconds=59))
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, ("TIME_EXIT_NOT_DUE",))
        self.assertEqual(parameters["time_exit_at"], "2026-10-06T12:15:00+00:00")

    def test_exits_at_captured_deadline(self):
        action, reasons, parameters = evaluate_time_exit(
            trade_state="OPEN", time_exit_at=FILL + timedelta(minutes=15), as_of=FILL + timedelta(minutes=15))
        self.assertEqual(action, "EXIT")
        self.assertEqual(reasons, ("TIME_EXIT_DUE",))
        self.assertEqual(parameters["exit_reason"], "TIME_EXIT")

    def test_closed_trade_never_emits_an_exit(self):
        action, reasons, _ = evaluate_time_exit(
            trade_state="CLOSED", time_exit_at=FILL, as_of=FILL + timedelta(hours=1))
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, ("TRADE_CLOSED",))

    def test_missing_policy_holds(self):
        action, reasons, _ = evaluate_time_exit(trade_state="OPEN", time_exit_at=None, as_of=FILL)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reasons, ("TIME_EXIT_NOT_CONFIGURED",))
