from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter


class ContextInstanceMembershipTest(unittest.TestCase):
    def test_provider_symbol_matches_canonical_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "context_structure_retrace_forward_state_compact.json"
            state_path.write_text(json.dumps({
                "setups": {
                    "setup-1": {
                        "setup_id": "setup-1",
                        "symbol": "BTCUSDm",
                        "direction": "LONG",
                        "pattern": "BULLISH_ENGULFING",
                        "target_completed": False,
                        "opportunities": [{
                            "economic_position_id": "position-1",
                            "entry_opportunity_id": "opportunity-1",
                            "symbol": "BTCUSDm",
                            "direction": "LONG",
                            "fill_timestamp": 1791148500,
                            "executable_paper_entry": 100.0,
                            "stop": 99.0,
                            "target": 102.0,
                            "geometry": {"stop_distance": 1.0, "target_R": 2.0},
                            "entry_mechanisms": ["DEPTH_ONLY"],
                            "status": "OPEN",
                            "reentry_type": "INITIAL",
                        }],
                    }
                }
            }))
            with patch.dict(os.environ, {"CONTEXT_RUNNER_STATE_DIR": directory}), \
                    patch("orchestration.adapters.context_structure_retrace.EPOCH_PATH", Path(directory) / "missing.json"):
                adapter = ContextStructureRetraceAdapter(
                    Path(directory), "2026-01-01T00:00:00Z",
                    {"active_instruments": ["BTCUSD"], "instance_policy": {"reentry_enabled": True}},
                )
                signals = adapter.discover_new_signals(set())

            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0].canonical_symbol, "BTCUSD")
            self.assertEqual(signals[0].broker_symbol_hint, "BTCUSDm")


if __name__ == "__main__":
    unittest.main()
