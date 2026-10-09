import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import context_structure_retrace_v2_forward as v2
from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter


class ContextV2ForwardTests(unittest.TestCase):
    def test_v2_has_independent_identity_and_state_paths(self):
        v1_fingerprint = v2._v1.FROZEN_DECISION_CODE_HASH
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CONTEXT_V2_RUNNER_STATE_DIR": directory}):
                v2._configure_runtime()
                self.assertEqual(v2._v1.VERSION, "CONTEXT_STRUCTURE_RETRACE_V2")
                self.assertEqual(v2._v1.SCHEMA_VERSION, "context-structure-retrace-forward-v2-schema-1")
                self.assertEqual(v2._v1.STATE.parent, Path(directory))
                self.assertNotEqual(v2.V2_DECISION_FINGERPRINT, v1_fingerprint)

    def test_v2_runtime_identity_is_not_a_freeze_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CONTEXT_V2_RUNNER_STATE_DIR": directory}):
                v2._configure_runtime()
                manifest = v2.runtime_identity()
                self.assertEqual(manifest["strategy_version"], "CONTEXT_STRUCTURE_RETRACE_V2")
                self.assertEqual(manifest["configuration"]["derived_from"], "CONTEXT_STRUCTURE_RETRACE_V1@V1")
                self.assertEqual(manifest["configuration"]["v2_contract_hash"], v2.V2_CONTRACT_HASH)
                self.assertEqual(manifest["configuration"]["v2_parameter_hash"], v2.V2_PARAMETER_HASH)
                self.assertEqual(manifest["identity_mode"], "RUNTIME_MUTABLE")
                self.assertNotIn("freeze_timestamp", manifest)

    def test_v2_adapter_identity_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "context_structure_retrace_v2_forward_state_compact.json"
            state_path.write_text(json.dumps({"setups": {}}))
            with patch.dict(os.environ, {"CONTEXT_V2_RUNNER_STATE_DIR": directory}), \
                    patch("orchestration.adapters.context_structure_retrace.EPOCH_PATH", Path(directory) / "missing.json"):
                adapter = ContextStructureRetraceAdapter(
                    Path(directory), "2026-01-01T00:00:00Z",
                    strategy_id="CONTEXT_STRUCTURE_RETRACE_V2", strategy_version="V2",
                    state_dir_env="CONTEXT_V2_RUNNER_STATE_DIR")
                self.assertEqual(adapter.strategy_id, "CONTEXT_STRUCTURE_RETRACE_V2")
                self.assertEqual(adapter.strategy_version, "V2")
                self.assertEqual(adapter.discover_new_signals(set()), [])


if __name__ == "__main__":
    unittest.main()
