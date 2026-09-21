import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import context_structure_retrace_phase7_observer as p7


def position(direction="LONG", status="OPEN"):
    entry = 100.0 if direction == "LONG" else 100.0
    stop = 99.0 if direction == "LONG" else 101.0
    return {"economic_position_id": "ep-1", "symbol": "TEST", "direction": direction,
            "setup_id": "s-1", "market_event_id": "m-1", "entry_opportunity_id": "o-1",
            "entry_attempt_id": "a-1", "fill_timestamp": 1000, "fill_timestamp_iso": p7.iso(1000),
            "executable_paper_entry": entry, "theoretical_entry": entry, "stop": stop,
            "target": 101.25 if direction == "LONG" else 98.75, "spread_at_fill": 0.1,
            "geometry": {"stop_distance": 1.0, "target_R": 1.25}, "status": status,
            "entry_mechanisms": ["DEPTH_ONLY"], "reentry_type": "INITIAL"}


def bars(*rows):
    return [{"time": t, "open": v, "high": h, "low": l, "close": v, "spread": 0} for t, v, h, l in rows] + [{"time": 99999, "open": 0, "high": 0, "low": 0, "close": 0, "spread": 0}]


class Phase7ObserverTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mktemp())
        self.tmp = patch.object(p7, "PHASE7_EVENTS", self.path)
        self.tmp.start()

    def tearDown(self):
        self.tmp.stop()
        if self.path.exists():
            os.unlink(self.path)

    def state(self):
        return p7.empty_state(p7.iso(0))

    def test_registration_does_not_count_registered_hypotheses_as_thresholds(self):
        s = self.state(); r = p7.register_position(s, position(), True)
        self.assertFalse(r["thresholds"]["+3.00R"]["reached"])

    def test_long_thresholds_are_post_entry_only_and_first_hit(self):
        s = self.state(); r = p7.register_position(s, position(), True)
        p7.observe_position(s, r, position(), bars((900, 104, 105, 99), (1060, 101, 102, 99), (1120, 102, 103, 100)), {"point": 0}, 0)
        self.assertTrue(r["thresholds"]["+3.00R"]["reached"])
        self.assertEqual(r["thresholds"]["+3.00R"]["first_timestamp"], p7.iso(1120))

    def test_short_threshold_math_uses_ask_side(self):
        s = self.state(); r = p7.register_position(s, position("SHORT"), True)
        p7.observe_position(s, r, position("SHORT"), bars((900, 96, 102, 95), (1060, 100, 102, 96)), {"point": 0.01}, 0)
        self.assertTrue(r["thresholds"]["+3.00R"]["reached"])
        self.assertTrue(r["thresholds"]["-1.00R"]["reached"])

    def test_post_exit_window_is_separate_from_v1_window(self):
        s = self.state(); pos = position(status="TARGET_HIT"); pos["exit_timestamp"] = 1050
        r = p7.register_position(s, pos, True)
        p7.observe_position(s, r, pos, bars((1020, 101, 102, 99), (1060, 104, 105, 100), (1120, 103, 104, 100)), {"point": 0}, 0)
        self.assertLess(r["v1_window_mfe_R"], r["shadow_mfe_R"])
        self.assertIsNotNone(r["post_exit_shadow_mfe_R"])

    def test_left_truncated_open_and_closed_reference(self):
        s = self.state(); open_row = p7.register_position(s, position(status="OPEN"), False)
        closed_row = p7.register_position(s, dict(position(status="TARGET_HIT"), economic_position_id="ep-2"), False)
        self.assertTrue(open_row["left_truncated"]); self.assertFalse(closed_row["left_truncated"])

    def test_append_event_is_idempotent(self):
        s = self.state(); event = {"type": "PHASE7_THRESHOLD_REACHED", "event_id": "same"}
        self.assertTrue(p7.append_event(s, event)); self.assertFalse(p7.append_event(s, event))
        self.assertEqual(s["counters"]["threshold_events"], 1)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_two_leg_accounting_is_total_one_risk(self):
        self.assertEqual(p7.two_leg_combined_R(1.0, 1.0), 1.0)
        self.assertEqual(p7.two_leg_combined_R(2.0, -1.0), 0.5)

    def test_order_isolation_is_read_only(self):
        audit = p7.order_isolation_audit()
        self.assertTrue(audit["pass"]); self.assertEqual(audit["disallowed_bridge_tools"], [])
