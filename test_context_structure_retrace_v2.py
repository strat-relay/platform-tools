from __future__ import annotations

import unittest
from unittest.mock import patch

from context_structure_retrace_v2 import MIN_PLANNED_R, select_target_candidates, v2_geometry


def base_geometry(*, direction: str = "LONG", entry: float = 100.0) -> dict:
    return {
        "stop": 99.0 if direction == "LONG" else 101.0,
        "stop_distance": 1.0,
        "extension_target": 101.0 if direction == "LONG" else 99.0,
        "opposing_structure": None,
        "effective_target": 101.0 if direction == "LONG" else 99.0,
        "signed_target_distance": 1.0,
        "target_direction_state": "TARGET_BEYOND_ENTRY",
        "target_R": 1.0,
    }


class ContextStructureRetraceV2Tests(unittest.TestCase):
    def test_accepts_exactly_one_r(self):
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base_geometry()):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertTrue(result["v2_eligible"])
        self.assertEqual(result["target_R"], MIN_PLANNED_R)

    def test_accepts_above_one_r(self):
        base = base_geometry()
        base["extension_target"] = 102.0
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertTrue(result["v2_eligible"])
        self.assertEqual(result["target_R"], 2.0)

    def test_rejects_below_one_r_without_fabricating_target(self):
        base = base_geometry()
        base["extension_target"] = 100.99
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertFalse(result["v2_eligible"])
        self.assertEqual(result["rejection_reason"], "RR_BELOW_MINIMUM")
        self.assertEqual(result["best_structural_target"], 100.99)

    def test_selects_farther_structural_target_when_nearest_is_below_one_r(self):
        base = base_geometry()
        base.update({"extension_target": 100.99, "opposing_structure": 102.0})
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertTrue(result["v2_eligible"])
        self.assertEqual(result["target_source"], "OPPOSING_STRUCTURE")
        self.assertEqual(result["effective_target"], 102.0)

    def test_short_calculation_is_directionally_correct(self):
        base = base_geometry(direction="SHORT")
        base["extension_target"] = 98.0
        base["opposing_structure"] = 99.0
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "SHORT", {}, 100.0, 0.0, None)
        self.assertTrue(result["v2_eligible"])
        self.assertEqual(result["effective_target"], 99.0)
        self.assertEqual(result["target_R"], 1.0)

    def test_invalid_risk_rejects(self):
        base = base_geometry()
        base["stop_distance"] = 0.0
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertFalse(result["v2_eligible"])
        self.assertEqual(result["rejection_reason"], "INVALID_RISK")

    def test_precision_is_not_ui_rounded(self):
        base = base_geometry()
        base["extension_target"] = 101.0000001
        with patch("context_structure_retrace_v2.v1_geometry", return_value=base):
            result = v2_geometry({}, "LONG", {}, 100.0, 0.0, None)
        self.assertAlmostEqual(result["target_R"], 1.0000001)

    def test_candidate_helper_preserves_structural_sources(self):
        rows = select_target_candidates({"extension_target": 101.0, "opposing_structure": 102.0}, "LONG", 100.0)
        self.assertEqual([row["source"] for row in rows], ["STRUCTURE_CAPPED_EXTENSION", "OPPOSING_STRUCTURE"])


if __name__ == "__main__":
    unittest.main()
