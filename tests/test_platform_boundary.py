"""Architecture checks for the extracted platform repository."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlatformBoundaryTests(unittest.TestCase):
    def test_bridge_implementation_is_not_present(self):
        self.assertFalse((ROOT / "mt5_bridge").exists())
        self.assertFalse((ROOT / "ea").exists())
        self.assertFalse((ROOT / "bridge.py").exists())

    def test_platform_sources_do_not_import_bridge_implementation(self):
        forbidden = {"mt5_bridge.server", "mt5_bridge.lifecycle", "bridge"}
        for path in ROOT.rglob("*.py"):
            if ".git" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
            self.assertTrue(forbidden.isdisjoint(imports), path)

    def test_mt5_contract_is_explicit(self):
        self.assertTrue((ROOT / "contracts/mt5_bridge/client.py").exists())
        self.assertTrue((ROOT / "contracts/mt5_bridge/errors.py").exists())
        self.assertTrue((ROOT / "contracts/mt5_bridge/canonical_request.py").exists())

    def test_ops_api_is_internal(self):
        self.assertTrue((ROOT / "control_api").exists())
        self.assertFalse((ROOT / "public_api").exists())
        self.assertFalse((ROOT / "portal_api").exists())

    def test_stage1_keeps_historical_strategy_paths(self):
        for relative in (
            "context_structure_retrace_forward.py",
            "liquidity_displacement.py",
            "orchestration/liquidity_instances.py",
            "trade_manager/central.py",
        ):
            self.assertTrue((ROOT / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
