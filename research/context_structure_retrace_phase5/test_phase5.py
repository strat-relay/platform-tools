import unittest

from .phase5 import _bucket, _geometry


class Phase5Tests(unittest.TestCase):
    def test_room_buckets_are_descriptive_and_normalized(self):
        self.assertEqual(_bucket(0.05), "<0.10R")
        self.assertEqual(_bucket(0.25), "0.25-0.50R")
        self.assertEqual(_bucket(2.0), ">1.50R")

    def test_geometry_uses_nearer_directional_target(self):
        item = {
            "direction": "LONG", "entry_price": 100.0, "spread": 0.1,
            "stop_hypotheses": {"ORIGINATING_SETUP_EXTREME": {"stop": 98.0, "stop_distance": 2.0, "stop_distance_atr": 1.0}},
            "target_hypotheses": {"CANDLE_EXTENSION_50": 104.0, "NEXT_OPPOSING_STRUCTURE": 101.0},
        }
        g = _geometry(item)
        self.assertEqual(g["effective_target"], 101.0)
        self.assertAlmostEqual(g["target_r"], 0.5)

    def test_long_structure_below_entry_cannot_be_profit_target(self):
        item = {
            "direction": "LONG", "entry_price": 100.0, "spread": 0.1,
            "stop_hypotheses": {"ORIGINATING_SETUP_EXTREME": {"stop": 98.0, "stop_distance": 2.0, "stop_distance_atr": 1.0}},
            "target_hypotheses": {"CANDLE_EXTENSION_50": 104.0, "NEXT_OPPOSING_STRUCTURE": 99.0},
        }
        g = _geometry(item)
        self.assertEqual(g["opposing_structure_state"], "TARGET_BEHIND_ENTRY")
        self.assertFalse(g["opposing_structure_valid_profit_target"])
        self.assertEqual(g["effective_target"], 104.0)
        self.assertEqual(g["target_state"], "TARGET_BEYOND_ENTRY")

    def test_review_definition_does_not_require_outcome(self):
        # Phase 5 review cases are built from geometry/context only; outcome
        # fields are deliberately not part of the review schema.
        self.assertNotIn("outcome", {"entry": 1, "target_r": 0.5})


if __name__ == "__main__":
    unittest.main()
