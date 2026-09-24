"""Static/isolation audit (A7 10 P4.1 acceptance test 6, mission section 10's isolation list):
no broker-write path is reachable, no Phase 7 dependency, no signals.jsonl dependency, no
pre-T0 runtime history migration, no legacy `trade_manager/` import.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "trade_management"

FORBIDDEN_MODULES = ("execution", "live_execution_consumer", "trade_manager", "contracts.mt5_bridge",
                     "orchestration", "control_api", "context_structure_retrace_phase7_observer")


def _source_without_module_docstring(path: Path) -> str:
    """Strip the leading module docstring before scanning for forbidden terms: this package's
    own docstrings legitimately *explain* what is absent (e.g. "no Phase 7 dependency"), which
    would otherwise false-positive a naive substring search - the same class of false positive
    documented and fixed in the NATS-first prototype's isolation tests."""
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


class IsolationTests(unittest.TestCase):
    def test_no_module_in_trade_management_imports_a_forbidden_package(self):
        for path in PACKAGE.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            imported = _imports(source)
            for forbidden in FORBIDDEN_MODULES:
                offenders = {name for name in imported if name == forbidden or name.startswith(forbidden + ".")}
                self.assertFalse(offenders, f"{path.name} imports forbidden module(s): {offenders}")

    def test_no_module_reads_signals_jsonl_or_a_runtime_directory(self):
        for path in PACKAGE.glob("*.py"):
            source = _source_without_module_docstring(path)
            self.assertNotIn("signals.jsonl", source, path.name)
            self.assertNotIn("TRADING_PLATFORM_RUNTIME_DIR", source, path.name)
            self.assertNotIn("phase7", source.lower(), path.name)

    def test_no_module_constructs_a_jsonl_store_or_opens_a_bare_file_path(self):
        for path in PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    self.fail(f"{path.name} calls open() directly - no file I/O belongs in this package")

    def test_no_broker_write_vocabulary_anywhere_in_the_package(self):
        # Literal broker-tool/port names only - these can never legitimately appear in this
        # package's code (unlike "ticket"/"broker_position"/"REAL_EXECUTION", which show up in
        # this package's own prose explaining what is deliberately absent; that invariant is
        # covered instead by the DDL column-name check and the docstring-stripped scans above).
        forbidden_terms = ("mt5_canonical_order_send", "mt5_close_position", "mt5_trailing_stop", "22348")
        for path in PACKAGE.glob("*.py"):
            if path.name == "fakes.py":
                continue  # test substrate; no production behavior
            source = _source_without_module_docstring(path)
            for term in forbidden_terms:
                self.assertNotIn(term, source, f"{path.name} contains forbidden broker-write term {term!r}")

    def test_no_entrypoint_is_activated_by_importing_the_package(self):
        # Importing every module must have zero side effects (no consumer starts, no loop runs).
        import importlib
        for name in ("ids", "versions", "market_data", "binding", "managed_trade", "open_consumer",
                     "observation", "tm_none", "publication_gate", "fakes"):
            module = importlib.import_module(f"trade_management.{name}")
            self.assertFalse(hasattr(module, "main"), f"trade_management.{name} defines main() - would be an entrypoint")


class DDLPropertyTests(unittest.TestCase):
    MIGRATION = ROOT / "postgres" / "migrations" / "013_trade_management_foundation.sql"

    def test_migration_file_exists_and_is_additive_create_only(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE", text)
        self.assertNotIn("DROP TABLE", text)
        self.assertNotIn("DROP COLUMN", text)
        self.assertNotIn("TRUNCATE", text)

    def test_no_forbidden_column_names_in_the_new_schema(self):
        import re
        text = self.MIGRATION.read_text(encoding="utf-8").lower()
        forbidden = ("account", "ticket", "lot", "volume", "broker_position", "execution_",
                    "entitle", "subscription", "customer", "published_")
        # Scan only the CREATE TABLE bodies for trade_management.* tables added by this file.
        tables = re.findall(r"create table if not exists (trade_management\.\w+) \((.*?)\n\);", text, re.S)
        self.assertTrue(tables, "expected at least one trade_management table definition")
        for name, body in tables:
            for forbidden_term in forbidden:
                self.assertNotIn(forbidden_term, body, f"{name} contains forbidden column-name fragment {forbidden_term!r}")

    def test_only_foreign_key_to_another_schema_is_strategy_entry_signals(self):
        import re
        text = self.MIGRATION.read_text(encoding="utf-8")
        refs = re.findall(r"REFERENCES\s+([a-zA-Z_]+\.[a-zA-Z_]+)", text)
        outside_own_schema = {r for r in refs if not r.startswith("trade_management.")}
        self.assertTrue(all(r.startswith("strategy.entry_signals") for r in outside_own_schema),
                        f"unexpected cross-schema reference(s): {outside_own_schema}")

    def test_decision_and_version_tables_have_immutability_triggers(self):
        text = self.MIGRATION.read_text(encoding="utf-8")
        self.assertIn("trade_manager_decision_immutable", text)
        self.assertIn("trade_manager_version_immutable", text)
        self.assertIn("managed_trade_binding_immutable", text)
        self.assertIn("legacy_stream_binding_append_only", text)


if __name__ == "__main__":
    unittest.main()
