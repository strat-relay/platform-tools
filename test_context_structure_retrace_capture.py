from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research.context_structure_retrace_capture import capture


class ContextRetraceCaptureTests(unittest.TestCase):
    def test_capture_is_hashed_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bars.jsonl"
            source.write_text('{"bar": 1}\n', encoding="utf-8")
            destination = root / "capture-1"
            manifest = capture(destination, (("market_data.jsonl", source),), capture_id="capture-1")
            self.assertEqual(manifest["schema"], "context-v1-parity-capture-v1")
            self.assertEqual(manifest["artifacts"][0]["bytes"], source.stat().st_size)
            self.assertEqual((destination / "market_data.jsonl").read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                capture(destination, (("market_data.jsonl", source),))

    def test_manifest_declares_research_only_replay_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "state.json"
            source.write_text("{}", encoding="utf-8")
            destination = root / "capture-2"
            capture(destination, (("state.json", source),))
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["production_writes"])
            self.assertTrue(manifest["replay_contract"]["live_state_must_be_immutable"])


if __name__ == "__main__":
    unittest.main()
