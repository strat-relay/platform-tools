"""Static isolation audit for `trade_management/runtime/` specifically (mission section 10):
no signals.jsonl dependency, no Phase 7 dependency, no execution/live_execution_consumer/
trade_manager/orchestration/control_api import, no broker-write path reachable. This is
deliberately separate from `tests/test_trade_management_isolation.py`, which only scans
`trade_management/*.py` (the pure domain layer) - `runtime/` is the one place allowed to import
`contracts.mt5_bridge` (read-only) and `nats`, so it needs its own, slightly different audit.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "trade_management" / "runtime"

FORBIDDEN_MODULES = ("execution", "live_execution_consumer", "trade_manager", "orchestration",
                     "control_api", "context_structure_retrace_phase7_observer")


def _source_without_module_docstring(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if (tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        lines = source.splitlines(keepends=True)
        start, end = tree.body[0].lineno, tree.body[0].end_lineno
        lines = lines[:start - 1] + lines[end:]
        return "".join(lines)
    return source


def _all_imports(path: Path) -> set[str]:
    """Collects both module-level and function-scoped imports (several of this package's
    modules deliberately defer `import nats`/`from contracts.mt5_bridge import ...` to inside a
    function so the module itself stays importable without those packages present)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


class RuntimeIsolationTests(unittest.TestCase):
    def test_no_module_imports_a_forbidden_package(self):
        for path in PACKAGE.glob("*.py"):
            imported = _all_imports(path)
            for forbidden in FORBIDDEN_MODULES:
                offenders = {name for name in imported if name == forbidden or name.startswith(forbidden + ".")}
                self.assertFalse(offenders, f"{path.name} imports forbidden module(s): {offenders}")

    def test_no_module_references_signals_jsonl_phase7_or_a_runtime_directory(self):
        for path in PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("signals.jsonl", source, path.name)
            self.assertNotIn("TRADING_PLATFORM_RUNTIME_DIR", source, path.name)
            self.assertNotIn("phase7", source.lower(), path.name)

    def test_no_broker_write_tool_literal_anywhere(self):
        forbidden_terms = ("mt5_canonical_order_send", "mt5_close_position", "mt5_trailing_stop",
                           "mt5_market_order", "mt5_pending_order", "mt5_cancel_pending_order")
        for path in PACKAGE.glob("*.py"):
            if path.name == "fakes.py":
                continue
            source = _source_without_module_docstring(path)
            for term in forbidden_terms:
                self.assertNotIn(term, source, f"{path.name} contains forbidden term {term!r}")

    # Port 22348 rejection itself is proven behaviourally, not by a text scan (several modules'
    # own docstrings legitimately explain "never 22348" in prose, which a substring scan cannot
    # distinguish from a real reference): see
    # test_trade_management_runtime_config_and_activation.py::test_bridge_url_targeting_execution_port_fails_closed
    # and test_trade_management_runtime_market_data.py::test_build_bridge_client_refuses_the_execution_port.

    def test_market_data_adapter_only_ever_calls_quote_and_rates(self):
        source = (PACKAGE / "market_data_live.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_client_methods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "client":
                    called_client_methods.add(node.func.attr)
                elif isinstance(node.func.value, ast.Name) and node.func.value.id == "client":
                    called_client_methods.add(node.func.attr)
        self.assertLessEqual(called_client_methods, {"quote", "rates"})

    def test_no_execution_intent_or_broker_order_table_is_ever_written(self):
        for path in PACKAGE.glob("*.py"):
            if path.name == "fakes.py":
                continue
            source = _source_without_module_docstring(path)
            for forbidden in ("execution.execution_intents", "execution.intents", "execution.broker_attempts",
                              "execution.orders", "broker_position", "broker_order"):
                self.assertNotIn(forbidden, source, f"{path.name} references forbidden table/concept {forbidden!r}")

    def test_no_entrypoint_import_side_effect(self):
        import importlib
        for name in ("config", "activation", "market_data_live", "streams", "open_consumer_runtime",
                     "tm_none_runtime", "observation_runtime", "health", "fakes"):
            module = importlib.import_module(f"trade_management.runtime.{name}")
            self.assertFalse(hasattr(module, "main"), f"{name}.py defines main() - would be an entrypoint")


if __name__ == "__main__":
    unittest.main()
