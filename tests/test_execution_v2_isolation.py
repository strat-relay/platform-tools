"""Static/isolation audit for execution_v2 (mission sections 6, 14, 18): no legacy filesystem-IPC
execution package, no legacy trade_manager import, no signals.jsonl dependency, no live broker
port literal outside its own documented rejection/safety context, no direct network/socket call
in this package (the real bridge transport is explicitly out of scope - see execution_v2/__init__.py),
and importing the package has zero side effects.

`mt5_canonical_order_send` itself is NOT forbidden vocabulary here (unlike trade_management's
isolation test): it is the one legitimate tool identifier this package binds into a
WriteAuthorization for the bridge to independently verify - it never means "code path capable of
actually sending it" the way it would in a package with no execution mandate at all.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "execution_v2"
RUNTIME_PACKAGE = PACKAGE / "runtime"
BRIDGE_PACKAGE = ROOT / "mt5_bridge_fence"

FORBIDDEN_MODULES = ("trade_management", "trade_manager", "execution.models", "execution.storage",
                     "execution.risk_policy", "execution.adapters", "execution.demo",
                     "execution.demo_broker", "context_structure_retrace_phase7_observer")


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


def _imports(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _module_file(dotted_name: str) -> Path | None:
    base = ROOT / Path(*dotted_name.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _resolve_import(dotted_name: str, *, from_package: str, level: int) -> str:
    """Resolves an `import x.y` or `from .z import w` style reference (module name only, as it
    would appear in ast.ImportFrom.module / ast.Import.alias.name) to an absolute dotted module
    path, given the importing file's own package."""
    if level == 0:
        return dotted_name
    parts = from_package.split(".")
    base_parts = parts[:len(parts) - level + 1] if level <= len(parts) else []
    if dotted_name:
        return ".".join(base_parts + dotted_name.split("."))
    return ".".join(base_parts)


def _transitive_local_imports(start_dotted_name: str) -> set[str]:
    """BFS over this repository's own files only (never third-party/stdlib, which simply have no
    resolvable local file and are skipped) - a purely static substitute for walking
    sys.modules, immune to import-caching order between test files."""
    seen: set[str] = set()
    queue = [start_dotted_name]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_file(name)
        if path is None:
            continue
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    queue.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                resolved_base = _resolve_import(node.module or "", from_package=package, level=node.level)
                if node.level > 0 and node.module is None:
                    # `from . import x` / `from .. import x`: each imported name is itself a submodule.
                    for alias in node.names:
                        queue.append(f"{resolved_base}.{alias.name}" if resolved_base else alias.name)
                else:
                    queue.append(resolved_base)
    return seen


class IsolationTests(unittest.TestCase):
    def test_no_module_imports_a_forbidden_package(self):
        for path in PACKAGE.glob("*.py"):
            imported = _imports(path.read_text(encoding="utf-8"))
            for forbidden in FORBIDDEN_MODULES:
                offenders = {name for name in imported if name == forbidden or name.startswith(forbidden + ".")}
                self.assertFalse(offenders, f"{path.name} imports forbidden module(s): {offenders}")

    def test_no_module_imports_the_legacy_bare_execution_package(self):
        # "execution_v2" itself must not be caught by a naive substring/prefix check against
        # "execution" - only an exact "execution" top-level import (the legacy file-IPC package)
        # or "execution.<submodule>" is forbidden.
        for path in PACKAGE.glob("*.py"):
            imported = _imports(path.read_text(encoding="utf-8"))
            offenders = {name for name in imported if name == "execution" or
                        (name.startswith("execution.") and not name.startswith("execution_v2"))}
            self.assertFalse(offenders, f"{path.name} imports the legacy execution package: {offenders}")

    def test_no_module_references_signals_jsonl_or_a_runtime_directory(self):
        for path in PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("signals.jsonl", source, path.name)
            self.assertNotIn("TRADING_PLATFORM_RUNTIME_DIR", source, path.name)

    def test_no_module_opens_a_bare_file_path_or_a_socket(self):
        for path in PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("open", "socket"):
                    self.fail(f"{path.name} calls {node.func.id}() directly - no file/socket I/O "
                             f"belongs in this package; the bridge boundary is fully simulated")

    def test_port_22348_never_appears_outside_this_files_own_documented_guards(self):
        # 22348 (the live order-submission port) may appear ONLY inside a module's own docstring,
        # explaining that it is never used - never in code that could plausibly construct a
        # request toward it.
        for path in PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("22348", source, f"{path.name} references live order-submission port 22348 outside its docstring")

    def test_no_entrypoint_is_activated_by_importing_the_package(self):
        import importlib
        for name in ("ids", "fence", "bridge_fence_sim", "bridge_fence_errors", "bridge_fence_types",
                     "bridge_fence_verify", "risk", "intent", "worker", "reconcile", "fakes"):
            module = importlib.import_module(f"execution_v2.{name}")
            self.assertFalse(hasattr(module, "main"), f"execution_v2.{name} defines main() - would be an entrypoint")

    def test_fence_authority_never_hardcodes_a_signing_key(self):
        # The only literal key material anywhere in the package must be inside fence.py's own
        # from_env() failure-mode documentation/tests, never a usable default.
        source = _source_without_module_docstring(PACKAGE / "fence.py")
        self.assertNotIn('os.getenv("V2_FENCE_SIGNING_KEY", "', source,
                         "from_env() must not supply a default signing key")

    def test_no_production_module_imports_the_test_only_simulator(self):
        # The core remediation for the audit finding that production execution wiring used
        # BridgeFenceSimulator (mission CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION section
        # 2): a purely static, AST-based transitive import closure over this repository's own
        # files, starting from every production entrypoint module, checking whether
        # execution_v2/bridge_fence_sim.py is ever reached. Deliberately NOT based on
        # sys.modules/importlib side effects, which are unreliable here - an earlier test file in
        # the same process may have already imported bridge_fence_sim for its own (legitimate,
        # test-only) purposes, which would hide a real violation from any cache-based check.
        production_modules = ("execution_v2.runtime.__main__", "execution_v2.runtime.service",
                              "execution_v2.runtime.config", "execution_v2.runtime.consumer",
                              "execution_v2.runtime.bridge_client", "execution_v2.runtime.health",
                              "mt5_bridge_fence.boundary", "mt5_bridge_fence.store",
                              "mt5_bridge_fence.http_server")
        forbidden = "execution_v2.bridge_fence_sim"
        for name in production_modules:
            reached = _transitive_local_imports(name)
            self.assertNotIn(forbidden, reached,
                            f"{name} transitively imports the test-only simulator {forbidden}")

    def test_runtime_package_source_never_mentions_the_simulator_class_name_outside_its_own_docstring(self):
        # A module's own top-level docstring MAY legitimately name BridgeFenceSimulator to
        # explain that it is NOT used (exactly the same "explaining what is absent" pattern
        # `_source_without_module_docstring` exists to avoid false-positiving on elsewhere in
        # this test suite) - what must never happen is the name appearing in actual code, i.e.
        # an import or a reference that could construct or type against it.
        for path in RUNTIME_PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("BridgeFenceSimulator", source,
                            f"{path.relative_to(ROOT)} references BridgeFenceSimulator outside its module docstring")


class BridgeFenceIsolationTests(unittest.TestCase):
    """mt5_bridge_fence/ is the REAL bridge-side implementation - it deliberately DOES open
    sockets and a SQLite file (unlike execution_v2/, which must not), so it gets its own,
    narrower isolation rules rather than reusing IsolationTests' "no I/O at all" check."""

    def test_no_module_imports_a_forbidden_package(self):
        for path in BRIDGE_PACKAGE.glob("*.py"):
            imported = _imports(path.read_text(encoding="utf-8"))
            for forbidden in FORBIDDEN_MODULES + ("control_api", "orchestration"):
                offenders = {name for name in imported if name == forbidden or name.startswith(forbidden + ".")}
                self.assertFalse(offenders, f"{path.name} imports forbidden module(s): {offenders}")

    def test_port_22348_never_appears_outside_docstrings(self):
        for path in BRIDGE_PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("22348", source, f"{path.name} references live order-submission port 22348 outside its docstring")

    def test_http_server_defaults_to_loopback_only(self):
        source = (BRIDGE_PACKAGE / "http_server.py").read_text(encoding="utf-8")
        self.assertIn('bind_host: str = "127.0.0.1"', source)

    def test_no_entrypoint_is_activated_by_importing_the_package(self):
        import importlib
        for name in ("store", "boundary", "http_server"):
            module = importlib.import_module(f"mt5_bridge_fence.{name}")
            self.assertFalse(hasattr(module, "main"), f"mt5_bridge_fence.{name} defines main() - would be an entrypoint")


class DDLPropertyTests(unittest.TestCase):
    MIGRATION = ROOT / "postgres" / "migrations" / "016_execution_v2_foundation.sql"

    def test_migration_file_exists_and_is_additive_create_only(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE", text)
        self.assertNotIn("DROP TABLE", text)
        self.assertNotIn("DROP COLUMN", text)
        self.assertNotIn("TRUNCATE", text)

    def test_does_not_touch_the_legacy_execution_schema_or_p2_p4_tables(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertNotIn("ALTER TABLE strategy.", text)
        self.assertNotIn("ALTER TABLE trade_management.", text)
        self.assertNotIn("CREATE SCHEMA IF NOT EXISTS execution ", text)  # legacy bare `execution` schema

    def test_only_foreign_key_to_another_schema_is_strategy_entry_signals_or_own_schema(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        refs = re.findall(r"REFERENCES\s+([a-zA-Z_]+\.[a-zA-Z_]+)", text)
        outside_own_schema = {r for r in refs if not r.startswith("execution_v2.")}
        self.assertTrue(all(r.startswith("strategy.entry_signals") for r in outside_own_schema),
                        f"unexpected cross-schema reference(s): {outside_own_schema}")

    def test_intent_attempt_and_result_tables_have_immutability_triggers(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertIn("execution_intent_immutable", text)
        self.assertIn("execution_attempt_terminal_immutable", text)
        self.assertIn("execution_result_immutable", text)

    def test_assert_generation_function_is_defined(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertIn("assert_generation", text)
        self.assertIn("platform.assert_generation", text)


if __name__ == "__main__":
    unittest.main()
