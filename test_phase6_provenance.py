import json
import tempfile
import unittest
from pathlib import Path

import context_structure_retrace_forward as phase6
from live_execution_consumer import _source_health_ok
from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter


def position(pid="new", ts="2026-09-17T12:00:00+00:00"):
    return {"economic_position_id": pid, "fill_timestamp_iso": ts, "symbol": "EURUSDm",
            "status": "OPEN", "stop": 1.2, "target": 1.0,
            "geometry": {"stop_distance": .1}}


class Phase6ProvenanceTests(unittest.TestCase):
    def test_successful_read_provenance_uses_completed_candle_and_age(self):
        state = {"positions": {"new": position()}, "symbols": {}}
        phase6.persist_new_opportunity_provenance(state, set(), "EURUSDm", "2026-09-17T12:00:30+00:00", False)
        p = state["positions"]["new"]["provenance"]
        self.assertTrue(p["source_read_health"])
        self.assertEqual(p["source_market_data_timestamp"], "2026-09-17T12:00:00+00:00")
        self.assertEqual(p["source_data_age"], 30.0)
        self.assertFalse(p["gap_recovery"])

    def test_gap_recovery_is_explicit_and_not_healthy_for_real_gate(self):
        state = {"positions": {"new": position()}, "symbols": {}}
        phase6.persist_new_opportunity_provenance(state, set(), "EURUSDm", "2026-09-17T12:00:30+00:00", True)
        p = state["positions"]["new"]["provenance"]
        self.assertTrue(p["gap_recovery"])
        signal = {"provenance": p}
        self.assertEqual(_source_health_ok(signal), (True, "SOURCE_DATA_HEALTHY"))
        self.assertTrue(p["gap_recovery"])

    def test_stale_age_is_preserved_and_gate_rejects_it(self):
        state = {"positions": {"new": position()}, "symbols": {}}
        phase6.persist_new_opportunity_provenance(state, set(), "EURUSDm", "2026-09-17T14:01:00+00:00", False)
        p = state["positions"]["new"]["provenance"]
        self.assertEqual(p["source_data_age"], 7260.0)
        self.assertEqual(_source_health_ok({"provenance": p}), (False, "STRATEGY_SOURCE_DATA_UNHEALTHY"))

    def test_existing_opportunity_is_not_backfilled(self):
        old = position("old")
        state = {"positions": {"old": old}, "symbols": {}}
        phase6.persist_new_opportunity_provenance(state, {"old"}, "EURUSDm", "2026-09-17T12:00:30+00:00", False)
        self.assertNotIn("provenance", state["positions"]["old"])

    def test_missing_provenance_remains_fail_closed(self):
        self.assertEqual(_source_health_ok({"provenance": {}}), (False, "STRATEGY_SOURCE_DATA_UNHEALTHY"))

    def test_adapter_propagates_producer_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = {"setups": {"s": {"setup_id": "s", "symbol": "EURUSDm", "direction": "LONG",
                "market_event_id": "m", "provenance": {}, "opportunities": [
                    {**position("p"), "fill_timestamp": 1789646400, "entry_opportunity_id": "o", "executable_paper_entry": 1.1,
                     "stop": 1.2, "target": 1.0, "geometry": {"stop_distance": .1},
                     "provenance": {"source_read_health": True, "source_data_age": 2.0,
                                    "source_market_data_timestamp": "2026-09-17T12:00:00+00:00", "gap_recovery": False}}
                ]}}}
            (root / "context_structure_retrace_forward_state.json").write_text(json.dumps(state))
            signals = ContextStructureRetraceAdapter(root, "2026-09-17T00:00:00+00:00").discover_new_signals(set())
            self.assertEqual(len(signals), 1)
            self.assertTrue(signals[0].provenance["source_read_health"])
            self.assertEqual(signals[0].provenance["source_data_age"], 2.0)

    def test_decision_fingerprint_unchanged(self):
        self.assertEqual(phase6.decision_code_hash(), phase6.FROZEN_DECISION_CODE_HASH)


if __name__ == "__main__":
    unittest.main()
