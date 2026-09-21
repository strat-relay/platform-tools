#!/usr/bin/env python3
"""Inventory the existing strategy code for the Strategy Studio migration map.

READ-ONLY.  Parses (never imports/executes) a fixed set of strategy modules with
``ast`` and writes:

    docs/strategy_studio/data/strategy_code_inventory.csv   one row per function
    docs/strategy_studio/data/inventory_summary.json        aggregate findings

    python3 docs/strategy_studio/tools/inventory_strategy_code.py

It does not touch runtime state, brokers, MT5, PostgreSQL or NATS, and it changes
no strategy file (frozen strategies are hash-guarded).  The signals it extracts are
*evidence for classification*, not the classification itself (that is a reviewed
judgement in ``10_CURRENT_STRATEGY_MIGRATION.md``):

  forward_scan     loop that indexes bars after the anchor bar (``range(i + 1``) or a
                   variable named ``future*`` -> lookahead-sensitive from the anchor
  as_of_param      function accepts an ``as_of`` boundary (causality discipline)
  magic_numbers    distinct non-trivial numeric literals in the body (parameterisation
                   candidates; 0, 1, -1, 2 and small indices are ignored)
  timeframes       timeframe literals referenced (M1..W1)
  reason_codes     UPPER_SNAKE string literals that look like decision/reason codes
  helpers          calls to shared building blocks (ema, atr, swings, patterns ...)
"""
from __future__ import annotations

import ast
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "docs" / "strategy_studio" / "data"

MODULES = [
    "paper_engine.py", "liquidity_displacement.py", "liquidity_displacement_forward.py",
    "liquidity_displacement_entry_forward.py", "orchestration/liquidity_instances.py",
    "context_structure_retrace_forward.py", "context_structure_retrace_compact_state.py",
    "context_structure_retrace_phase7_observer.py",
    *[f"context_structure_retrace/{n}.py" for n in (
        "attention", "data", "indicators", "ledger", "normalization", "outcomes", "patterns",
        "provenance", "replay", "retracement", "schema", "sr", "structures")],
    "research/multitimeframe_liquidity_sniper/engine.py", "research/multitimeframe_liquidity_sniper/state_machine.py",
    "research/multitimeframe_liquidity_sniper/entry.py", "research/multitimeframe_liquidity_sniper/neutral.py",
    "research/multitimeframe_liquidity_sniper/validation.py",
    "research/multitimeframe_structure_sniper/engine.py", "research/multitimeframe_structure_sniper/events.py",
]

TIMEFRAME = re.compile(r"^(M1|M5|M15|M30|H1|H4|D1|W1)$")
REASON = re.compile(r"^[A-Z][A-Z0-9]+(_[A-Z0-9]+){1,}$")
HELPERS = {"ema", "sma", "rsi", "atr", "swings", "confirmed_swings", "detect_patterns", "build_zones", "sr_context",
           "ema_context", "measure_retracement", "aggregate_bars", "structure_direction", "build_structure_map",
           "classify_h4_context", "classify_h1_context", "make_m15_setup", "detect_m15_setups", "_atr", "_candle",
           "bar_end", "completed_candles", "trendline_candidates", "channel_candidates", "attention_layer"}
TRIVIAL = {0, 1, -1, 2, 0.0, 1.0, -1.0, 2.0, 3, 4, 5, 10, 100, 1000}
NOISE_STRINGS = {"NONE", "LONG", "SHORT", "UP", "DOWN", "HIGH", "LOW", "OPEN", "CLOSE"}


def analyse(path: str) -> list[dict]:
    src = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=path)
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        numbers, tfs, reasons, helpers = set(), set(), set(), set()
        forward = False
        for child in ast.walk(node):
            if isinstance(child, ast.Constant):
                v = child.value
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v not in TRIVIAL:
                    numbers.add(v)
                elif isinstance(v, str):
                    if TIMEFRAME.match(v):
                        tfs.add(v)
                    elif REASON.match(v) and v not in NOISE_STRINGS and len(v) > 6:
                        reasons.add(v)
            elif isinstance(child, ast.Call):
                fn = child.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in HELPERS:
                    helpers.add(name)
                if name == "range" and child.args:
                    first = child.args[0]
                    text = ast.unparse(first)
                    if re.search(r"\b(i|index|start|disp_i|start_i)\s*\+\s*1\b", text) and len(child.args) > 1:
                        forward = True
            elif isinstance(child, ast.Name) and child.id.lower().startswith(("future", "forward_")):
                forward = True
        args = [a.arg for a in node.args.args + node.args.kwonlyargs]
        rows.append({
            "module": path, "function": node.name, "line": node.lineno, "loc": end - node.lineno + 1,
            "as_of_param": int("as_of" in args or "as_of_ts" in args), "forward_scan": int(forward),
            "magic_numbers": len(numbers), "timeframes": "|".join(sorted(tfs)),
            "reason_codes": "|".join(sorted(reasons))[:400], "helpers": "|".join(sorted(helpers)),
        })
    return rows


def main() -> None:
    rows: list[dict] = []
    for path in MODULES:
        if (ROOT / path).exists():
            rows.extend(analyse(path))
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "strategy_code_inventory.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["module"], r["line"])))

    by_module: dict[str, dict] = defaultdict(lambda: {"functions": 0, "loc": 0, "magic_numbers": 0, "forward_scan": 0, "as_of_param": 0})
    for r in rows:
        m = by_module[r["module"]]
        m["functions"] += 1
        m["loc"] += r["loc"]
        m["magic_numbers"] += r["magic_numbers"]
        m["forward_scan"] += r["forward_scan"]
        m["as_of_param"] += r["as_of_param"]
    reason_codes: Counter = Counter()
    for r in rows:
        for code in filter(None, r["reason_codes"].split("|")):
            reason_codes[code] += 1
    summary = {
        "modules_scanned": len(by_module), "functions": len(rows),
        "functions_with_forward_scan": sorted(f"{r['module']}::{r['function']}" for r in rows if r["forward_scan"]),
        "functions_with_as_of_param": sum(r["as_of_param"] for r in rows),
        "distinct_reason_like_codes": len(reason_codes),
        "reason_codes": sorted(reason_codes),
        "by_module": dict(sorted(by_module.items())),
    }
    (OUT / "inventory_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"by_module", "reason_codes"}}, indent=2))


if __name__ == "__main__":
    main()
