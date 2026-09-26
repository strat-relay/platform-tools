"""The Context runner, its compaction module and the orchestrator adapter keep runtime artifacts in
CONTEXT_RUNNER_STATE_DIR, so the runner can execute from an immutable release image while its
state stays on the persistent volume. Unset, everything stays next to the code (previous behaviour)."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULES = ("context_structure_retrace_forward", "context_structure_retrace_compact_state")
STATE_FILE = "context_structure_retrace_forward_state_compact.json"


def _reload():
    return [importlib.reload(importlib.import_module(m)) for m in MODULES]


class StateDirTests(unittest.TestCase):
    def tearDown(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTEXT_RUNNER_STATE_DIR", None)
            _reload()

    def test_state_artifacts_follow_the_env_and_code_does_not(self):
        with patch.dict(os.environ, {"CONTEXT_RUNNER_STATE_DIR": "/work"}):
            fwd, compact = _reload()
            for path in (fwd.STATE, fwd.EVENTS, fwd.HEARTBEAT, fwd.PID, fwd.MANIFEST, fwd.SUMMARY, fwd.LEGACY_STATE,
                         compact.COMPACT_STATE, compact.FULL_STATE):
                self.assertEqual(path.parent, Path("/work"), path)
            self.assertEqual(fwd.ROOT, ROOT)                                     # source/fingerprint stay on the code
            self.assertEqual(fwd.decision_code_hash(), fwd.FROZEN_DECISION_CODE_HASH)
            from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter
            adapter = ContextStructureRetraceAdapter(ROOT, "freeze")
            self.assertEqual(adapter.state_path, Path("/work") / STATE_FILE)
            self.assertEqual(adapter.manifest_path.parent, Path("/work"))

    def test_default_is_the_code_directory(self):
        os.environ.pop("CONTEXT_RUNNER_STATE_DIR", None)
        fwd, compact = _reload()
        self.assertEqual(fwd.STATE, ROOT / STATE_FILE)
        self.assertEqual(compact.COMPACT_STATE, ROOT / STATE_FILE)


if __name__ == "__main__":
    unittest.main()
