#!/usr/bin/env python3
"""Read-only static audit of file-based IPC in production modules.

Parses (never imports or executes) the production Python modules and records every
call site that reads, writes, appends, replaces, unlinks or existence-checks a file.
It is the *static half* of the PRODUCTION_FILE_IPC=0 audit (see 17_FILE_IPC_ZERO_DEFINITION.md);
the *dynamic half* is the empty-runtime test defined there.

    python3 docs/migration/tools/audit_file_ipc.py inventory   # writes data/io_call_sites.csv
    python3 docs/migration/tools/audit_file_ipc.py audit       # exit 1 while violations exist

Heuristic by design: module-level constants (``RESUME_STATE = RUNTIME / "x.json"``) are
resolved one level so ``RESUME_STATE.read_text()`` reports its real target; instance
attributes (``self.path``) are reported unresolved.  A site is a *candidate IPC site*
when its resolved target or receiver names runtime/state/queue files (.json, .jsonl,
.pid, .stop, runtime dirs, /tmp).  Reads of *configuration* and writes to *research*
outputs are permitted by the allow-list below; everything else is a finding.

It touches no runtime state, broker, MT5, PostgreSQL, NATS or Kubernetes.
"""
from __future__ import annotations

import ast
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "docs" / "migration" / "data"

PRODUCTION_GLOBS = [
    "signal_orchestrator.py", "live_execution_consumer.py", "platform_runtime.py",
    "context_structure_retrace_forward.py", "context_structure_retrace_compact_state.py",
    "context_structure_retrace_phase7_observer.py", "liquidity_displacement_forward.py",
    "liquidity_displacement_entry_forward.py", "liquidity_displacement.py",
    "orchestration/*.py", "orchestration/adapters/*.py", "orchestration/brokers/*.py",
    "execution/*.py", "trade_manager/*.py", "control_api/*.py", "core/**/*.py", "contracts/**/*.py",
    "scripts/*.py",
]
# Files whose purpose is not runtime IPC even though they touch files.
NON_IPC_MODULES = {
    "trade_manager/replay.py", "trade_manager/discrepancy.py",          # offline analysis of a fixture
}
# Modules whose *job* is to be the file transport/storage layer: receivers are parameters
# (``path``, ``self.path``), so the heuristic cannot resolve them, but every call site is IPC.
IO_PRIMITIVE_MODULES = {
    "orchestration/storage.py", "execution/storage.py", "trade_manager/central.py",
    "trade_manager/storage.py", "trade_manager/observation_storage.py", "trade_manager/state.py",
    "trade_manager/prospective.py", "trade_manager/fanout.py", "orchestration/replay_guard.py",
}
CONFIG_TARGETS = re.compile(r"(orchestration/config|config/|platform\.json|risk_policy\.json|tradeability_policy\.json|CONFIG_PATH|RISK_POLICY_PATH|policy_path)")
IPC_HINT = re.compile(r"(runtime|RUNTIME|CENTRAL|\.jsonl|\.json|\.pid|\.stop|/tmp|_PATH|STATE|EVENTS|HEARTBEAT|\bPID\b|STOP|MANIFEST|checkpoint|\.tmp)")

READ_METHODS = {"read_text", "read_bytes"}
WRITE_METHODS = {"write_text", "write_bytes"}
UNLINK = {"unlink"}
REPLACE = {"replace", "rename"}


def production_files() -> list[Path]:
    seen: list[Path] = []
    for pattern in PRODUCTION_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and not rel.startswith("tests/") and not Path(rel).name.startswith("test_") and rel not in seen:
                seen.append(p)
    return seen


def module_constants(tree: ast.Module) -> dict[str, str]:
    table: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                table[node.targets[0].id] = ast.unparse(node.value)
            except Exception:
                pass
    return table


def resolve(expr: ast.AST, consts: dict[str, str]) -> tuple[str, str]:
    receiver = ast.unparse(expr)
    if isinstance(expr, ast.Name) and expr.id in consts:
        return receiver, consts[expr.id]
    return receiver, ""


def open_mode(call: ast.Call) -> str:
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        return str(call.args[1].value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return "r"


def scan(path: Path) -> list[dict]:
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    consts = module_constants(tree)
    rows: list[dict] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            self.stack.append(node.name); self.generic_visit(node); self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):  # noqa: N802
            self.stack.append(node.name); self.generic_visit(node); self.stack.pop()

        def add(self, node, op, receiver, target):
            rows.append({"module": rel, "line": node.lineno, "function": ".".join(self.stack) or "<module>",
                         "op": op, "receiver": receiver[:90], "resolved_target": target[:110]})

        def visit_Call(self, node):  # noqa: N802
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "open" and node.args:
                receiver, target = resolve(node.args[0], consts)
                mode = open_mode(node)
                self.add(node, "APPEND" if "a" in mode else "WRITE" if "w" in mode or "x" in mode else "READ", receiver, target)
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
                receiver, target = resolve(fn.value, consts)
                if name == "open":
                    mode = open_mode(node)
                    self.add(node, "APPEND" if "a" in mode else "WRITE" if "w" in mode else "READ", receiver, target)
                elif name in READ_METHODS:
                    self.add(node, "READ", receiver, target)
                elif name in WRITE_METHODS:
                    self.add(node, "WRITE", receiver, target)
                elif name in UNLINK:
                    self.add(node, "UNLINK", receiver, target)
                elif name in REPLACE and (receiver.startswith("os") or receiver.startswith("tmp") or "Path" in receiver or receiver in consts):
                    self.add(node, "REPLACE", receiver, target)
                elif name == "exists" and IPC_HINT.search(receiver + target):
                    self.add(node, "EXISTS", receiver, target)
                elif name == "fsync":
                    self.add(node, "FSYNC", receiver, target)
            self.generic_visit(node)

    V().visit(tree)
    return rows


def classify(row: dict) -> str:
    text = row["receiver"] + " " + row["resolved_target"]
    if row["module"] in NON_IPC_MODULES:
        return "ALLOWED_OFFLINE"
    if row["op"] == "FSYNC":
        return "DURABILITY_CALL"
    if CONFIG_TARGETS.search(text) and row["op"] in {"READ", "EXISTS"}:
        return "ALLOWED_CONFIG_READ"
    if IPC_HINT.search(text) or row["module"] in IO_PRIMITIVE_MODULES:
        return "IPC_CANDIDATE"
    return "UNRESOLVED"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "inventory"
    rows: list[dict] = []
    for p in production_files():
        for r in scan(p):
            r["classification"] = classify(r)
            rows.append(r)
    OUT.mkdir(parents=True, exist_ok=True)
    fields = ["module", "line", "function", "op", "receiver", "resolved_target", "classification"]
    counts = Counter(r["classification"] for r in rows)
    by_module: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_module[r["module"]][r["classification"]] += 1
    summary = {
        "files_scanned": len(production_files()), "call_sites": len(rows),
        "by_classification": dict(counts),
        "ipc_candidates_by_module": {m: c["IPC_CANDIDATE"] for m, c in sorted(by_module.items()) if c["IPC_CANDIDATE"]},
        "unresolved_by_module": {m: c["UNRESOLVED"] for m, c in sorted(by_module.items()) if c["UNRESOLVED"]},
        "fsync_call_sites": sorted(f"{r['module']}:{r['line']}" for r in rows if r["op"] == "FSYNC"),
        "by_op": dict(Counter(r["op"] for r in rows)),
    }
    if mode == "inventory":
        with (OUT / "io_call_sites.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(sorted(rows, key=lambda r: (r["module"], r["line"])))
        (OUT / "io_audit_baseline.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(json.dumps({k: v for k, v in summary.items() if k not in {"ipc_candidates_by_module", "unresolved_by_module"}}, indent=2))
        return 0
    # audit: any IPC candidate is a violation of PRODUCTION_FILE_IPC=0 until migrated
    violations = [r for r in rows if r["classification"] == "IPC_CANDIDATE"]
    print(f"PRODUCTION_FILE_IPC audit (static): {len(violations)} IPC candidate call site(s) in {len({r['module'] for r in violations})} module(s)")
    for m, c in summary["ipc_candidates_by_module"].items():
        print(f"  {c:4d}  {m}")
    print("Unresolved sites need manual review:", sum(summary["unresolved_by_module"].values()))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
