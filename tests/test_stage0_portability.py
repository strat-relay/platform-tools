from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_runtime import require_trading_platform_runtime, trading_platform_runtime_dir


class Stage0RuntimeDirectoryTests(unittest.TestCase):
    def test_default_matches_historical_checkout_runtime(self):
        root = Path("/future/platform")
        self.assertEqual(trading_platform_runtime_dir(root=root, environ={}), root / "runtime")

    def test_explicit_directory_is_honored_without_resetting_state(self):
        root = Path("/future/platform")
        configured = "/existing/platform-runtime"
        self.assertEqual(trading_platform_runtime_dir(root=root, environ={"TRADING_PLATFORM_RUNTIME_DIR": configured}), Path(configured))

    def test_relative_directory_is_resolved_from_checkout_root(self):
        root = Path("/future/platform")
        self.assertEqual(trading_platform_runtime_dir(root=root, environ={"TRADING_PLATFORM_RUNTIME_DIR": "shared-runtime"}), root / "shared-runtime")

    def test_real_mode_missing_runtime_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist"
            with self.assertRaisesRegex(RuntimeError, "TRADING_PLATFORM_RUNTIME_DIR_UNAVAILABLE"):
                require_trading_platform_runtime(mode="REAL_EXECUTION", root=Path(td),
                                                  environ={"TRADING_PLATFORM_RUNTIME_DIR": str(missing)})

    def test_real_mode_existing_runtime_does_not_mutate_generation(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"
            runtime.mkdir()
            generation = runtime / "orchestration" / "state.json"
            generation.parent.mkdir()
            generation.write_text('{"live_execution_resume_generation": 7}\n')
            resolved = require_trading_platform_runtime(mode="REAL_EXECUTION", root=Path(td), environ={
                "TRADING_PLATFORM_RUNTIME_DIR": str(runtime)})
            self.assertEqual(resolved, runtime)
            self.assertEqual(generation.read_text(), '{"live_execution_resume_generation": 7}\n')


class Stage0ConfigurationTests(unittest.TestCase):
    def test_canonical_request_golden_vector(self):
        from live_execution_consumer import canonical_request_fingerprint, canonical_request_text
        import json
        fixture = json.loads(Path("protocol/v1/fixtures/canonical_requests.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(canonical_request_text(fixture["request"]), fixture["canonical_request_text"])
        self.assertEqual(canonical_request_fingerprint(fixture["request"]), fixture["request_fingerprint"])

    def test_smoke_account_is_not_tracked_as_a_literal(self):
        source = Path("live_execution_consumer.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("REAL_SMOKE_ACCOUNT")', source)
        self.assertNotIn("Exness-MT5Real27", source)

    def test_read_once_and_wine_paths_have_unchanged_defaults_and_overrides(self):
        source = Path("liquidity_displacement_forward.py").read_text(encoding="utf-8")
        self.assertIn("MT5_WINE_BIN", source)
        self.assertIn("MT5_WINEPREFIX", source)
        self.assertIn("MT5_WINPY", source)
        self.assertIn("MT5_READ_ONCE_PATH", source)
        read_once = Path("mt5_read_once.py").read_text(encoding="utf-8")
        self.assertIn("MT5_TERMINAL_PATH", read_once)


if __name__ == "__main__":
    unittest.main()
