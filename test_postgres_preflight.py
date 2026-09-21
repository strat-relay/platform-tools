import unittest

from postgres.preflight import build_boundary, validate_state


class PostgreSQLPreflightTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "strategy_version": "CONTEXT_STRUCTURE_RETRACE_V1",
            "last_successful_read_at": "2026-09-17T16:54:41+00:00",
            "setups": {"s1": {"setup_id": "s1", "status": "FILLED", "target_completed": False, "opportunities": [{"entry_opportunity_id": "o1", "economic_position_id": "p1"}]}},
            "positions": {"p1": {"economic_position_id": "p1", "status": "OPEN"}},
        }
        self.manifest = {"strategy_version": "CONTEXT_STRUCTURE_RETRACE_V1", "configuration_hash": "cfg", "freeze_timestamp": "2026-09-16T05:00:00+00:00"}

    def test_explicit_boundary_required(self):
        with self.assertRaises(ValueError):
            build_boundary(self.state, self.manifest, cutoff=None, source_files={}, source_hashes={})

    def test_event_after_cutoff_is_rejected(self):
        result = validate_state(self.state, [{"setup_id": "s1", "event_time": "2026-09-17T16:55:00+00:00", "type": "SETUP_DETECTED"}], cutoff="2026-09-17T16:54:41+00:00")
        self.assertFalse(result["safe_to_import"])
        self.assertIn("EVENT_AFTER_CUTOFF", {item["kind"] for item in result["issues"]})

    def test_terminal_position_mismatch_is_rejected(self):
        result = validate_state(self.state, [{"economic_position_id": "p1", "event_time": "2026-09-17T16:54:40+00:00", "type": "TARGET_HIT"}], cutoff="2026-09-17T16:54:41+00:00")
        self.assertFalse(result["safe_to_import"])
        self.assertIn("POSITION_TERMINALITY_MISMATCH", {item["kind"] for item in result["issues"]})

    def test_coherent_fixture_passes(self):
        state = dict(self.state, positions={"p1": {"economic_position_id": "p1", "status": "TARGET_HIT"}})
        result = validate_state(state, [{"economic_position_id": "p1", "event_time": "2026-09-17T16:54:40+00:00", "type": "TARGET_HIT"}], cutoff="2026-09-17T16:54:41+00:00")
        self.assertTrue(result["safe_to_import"])


if __name__ == "__main__":
    unittest.main()
