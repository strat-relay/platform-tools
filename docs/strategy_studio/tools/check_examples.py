#!/usr/bin/env python3
"""Documentation linter for the Strategy Studio worked examples.

THIS IS NOT THE STRATEGY COMPILER.  The DSL is not implemented.  This script exists so
the illustrative YAML in docs/strategy_studio/examples/ cannot silently contradict
itself or the design docs.  It checks only what the examples promise:

  * every ``use:`` names a primitive in the seed catalogue, with only declared params
  * every ``$param`` / ``s.<stage>`` / ``f.<feature>`` / ``lv.<level>`` / ``d.<derived>``
    reference resolves
  * no stage (or derived value) references a LATER stage  (static lookahead check)
  * timeframe roles used exist; stage windows are positive integers
  * parameter defaults are inside their declared bounds
  * evaluation_mode rules (DETERMINISTIC has no review gate; HYBRID has >=1, each
    fail-closed; signal-emitting modes declare stop, target and exits)
  * ParameterSet values name declared parameters and respect bounds

    python3 docs/strategy_studio/tools/check_examples.py      # exit 1 on any finding

Reads YAML only; imports no repository code; contacts nothing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path.cwd()
EX = ROOT / "docs" / "strategy_studio" / "examples"
BUILTINS = {"min", "max", "abs", "beyond", "toward", "dir", "closes_through", "close"}
ROOTS = {"market", "inst", "direction"}
SKIP_KEYS = {"meta", "evidence_refs", "question", "origin", "reason", "notes"}
MODES = {"DETERMINISTIC", "HYBRID", "RESEARCH_ONLY"}
findings: list[str] = []


def bad(where: str, message: str) -> None:
    findings.append(f"{where}: {message}")


def strings(node, path=""):
    """yield (path, string) for every string leaf, skipping documentation-only keys"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in SKIP_KEYS:
                continue
            yield from strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def check_definition(path: Path, catalog: dict) -> dict:
    d = yaml.safe_load(path.read_text())
    w = path.name
    if d.get("schema") != "strategy-definition/1":
        bad(w, "schema must be strategy-definition/1")
    mode = d.get("evaluation_mode")
    if mode not in MODES:
        bad(w, f"evaluation_mode {mode!r} not in {sorted(MODES)}")
    roles = set(d.get("timeframes", {}))
    params = d.get("parameters", {})
    stages = d.get("setup", {}).get("stages", [])
    stage_ids = [s["id"] for s in stages]
    stage_index = {sid: i for i, sid in enumerate(stage_ids)}
    features, levels, derived = set(d.get("features", {})), set(d.get("levels", {})), set(d.get("derive", {}))
    if len(set(stage_ids)) != len(stage_ids):
        bad(w, "duplicate stage ids")

    # parameters: defaults within bounds
    for name, spec in params.items():
        lo, hi = spec.get("bounds", [None, None])
        default = spec.get("default")
        if default is None or lo is None:
            bad(w, f"parameter {name}: needs default and bounds")
        elif not (lo <= default <= hi):
            bad(w, f"parameter {name}: default {default} outside bounds [{lo},{hi}]")
        if spec.get("type") == "int" and isinstance(default, float):
            bad(w, f"parameter {name}: int parameter has float default")

    def check_use(node: dict, where: str) -> None:
        use = node.get("use")
        if use is None:
            return
        entry = catalog.get(use)
        if entry is None:
            bad(w, f"{where}: unknown primitive {use}")
            return
        for key in (node.get("params") or {}):
            if key not in (entry.get("params") or {}):
                bad(w, f"{where}: {use} has no parameter {key!r}")
        for key in (entry.get("params") or {}):
            if key not in (node.get("params") or {}):
                bad(w, f"{where}: {use} requires parameter {key!r} (no implicit defaults in a definition)")
        if node.get("role") and node["role"] not in roles:
            bad(w, f"{where}: timeframe role {node['role']!r} not declared")

    def walk_uses(node, where):
        if isinstance(node, dict):
            check_use(node, where)
            for k, v in node.items():
                if k in SKIP_KEYS:
                    continue
                walk_uses(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk_uses(v, f"{where}[{i}]")

    for key in ("levels", "features", "context", "filters", "setup", "derive"):
        walk_uses(d.get(key, {}), key)

    # roles on levels/features/stages
    for section in ("levels", "features"):
        for name, spec in d.get(section, {}).items():
            if spec.get("role") not in roles:
                bad(w, f"{section}.{name}: role {spec.get('role')!r} not declared")

    # stage ordering, windows, references
    for i, s in enumerate(stages):
        if s.get("role") not in roles:
            bad(w, f"stage {s['id']}: role {s.get('role')!r} not declared")
        if i == 0 and s.get("after"):
            bad(w, f"stage {s['id']}: first stage cannot have 'after'")
        if i > 0:
            after = s.get("after")
            if after not in stage_index:
                bad(w, f"stage {s['id']}: 'after' {after!r} is not a stage")
            elif stage_index[after] >= i:
                bad(w, f"stage {s['id']}: 'after' {after!r} is not EARLIER (forward reference)")
            within = (s.get("within") or {}).get("bars")
            if within is None:
                bad(w, f"stage {s['id']}: needs a bounded window (within.bars)")
            elif isinstance(within, str) and within.startswith("$"):
                spec = params.get(within[1:])
                if spec is None or spec.get("type") != "int" or spec["bounds"][0] < 1:
                    bad(w, f"stage {s['id']}: window {within} must be an int parameter with lower bound >= 1")
            elif not (isinstance(within, int) and within > 0):
                bad(w, f"stage {s['id']}: window must be a positive integer")

    def dep_index(text: str) -> int:
        idx = -1
        for m in re.finditer(r"\bs\.([a-z_0-9]+)", text):
            idx = max(idx, stage_index.get(m.group(1), 10**6))
        for m in re.finditer(r"\bd\.([a-z_0-9]+)", text):
            idx = max(idx, derived_dep.get(m.group(1), 10**6))
        return idx

    derived_dep: dict[str, int] = {}
    for name, spec in d.get("derive", {}).items():
        derived_dep[name] = max((dep_index(t) for _, t in strings(spec)), default=-1)

    for i, s in enumerate(stages):
        for p, text in strings({k: v for k, v in s.items() if k not in ("id", "role", "after", "within", "select")}):
            if dep_index(text) >= i:
                bad(w, f"stage {s['id']}: reference in {p} ({text!r}) depends on this or a later stage (lookahead)")

    # global reference resolution
    for p, text in strings(d):
        for m in re.finditer(r"\$([a-z_0-9]+)", text):
            if m.group(1) not in params:
                bad(w, f"{p}: undefined parameter ${m.group(1)}")
        for m in re.finditer(r"\bs\.([a-z_0-9]+)", text):
            if m.group(1) not in stage_index:
                bad(w, f"{p}: undefined stage s.{m.group(1)}")
        for m in re.finditer(r"\bf\.([a-z_0-9]+)", text):
            if m.group(1) not in features:
                bad(w, f"{p}: undefined feature f.{m.group(1)}")
        for m in re.finditer(r"\blv\.([a-z_0-9]+)", text):
            if m.group(1) not in levels:
                bad(w, f"{p}: undefined level lv.{m.group(1)}")
        for m in re.finditer(r"\bd\.([a-z_0-9]+)", text):
            if m.group(1) not in derived:
                bad(w, f"{p}: undefined derived value d.{m.group(1)}")
        for m in re.finditer(r"\b([a-z_]+)\(", text):
            if m.group(1) not in BUILTINS:
                bad(w, f"{p}: unknown function {m.group(1)}()")
        for m in re.finditer(r"\b(?:closes_through|close)\(\s*([a-z_]+)", text):
            if m.group(1) not in roles:
                bad(w, f"{p}: timeframe role {m.group(1)!r} not declared")
        for m in re.finditer(r"\b([a-z_]+)\.[a-z_]+", text):
            head = m.group(1)
            if head in ROOTS or head in {"s", "f", "lv", "d"}:
                continue

    # direction
    direction = d.get("direction", {})
    if direction.get("mode") == "DETECTED":
        if direction.get("from") not in stage_index:
            bad(w, "direction.from must name a stage")
    elif direction.get("mode") == "EVALUATE_EACH":
        if not set(direction.get("allowed", [])) <= {"LONG", "SHORT"} or not direction.get("allowed"):
            bad(w, "direction.allowed must be a non-empty subset of LONG/SHORT")
    else:
        bad(w, "direction.mode must be DETECTED or EVALUATE_EACH")

    # invalidation / exits / review gates / policy
    for inv in d.get("invalidation", []):
        if inv.get("active_from") not in stage_index:
            bad(w, f"invalidation {inv.get('id')}: active_from must be a stage")
    inv_ids = {inv["id"] for inv in d.get("invalidation", [])}
    for ex in d.get("exits", []):
        if ex.get("kind") == "INVALIDATION_EXIT" and ex.get("ref") not in inv_ids:
            bad(w, f"exit references unknown invalidation {ex.get('ref')!r}")
    gates = d.get("review_gates", [])
    if mode == "DETERMINISTIC" and gates:
        bad(w, "DETERMINISTIC definitions must not declare review_gates")
    if mode == "HYBRID" and not gates:
        bad(w, "HYBRID definitions must declare at least one review gate")
    for g in gates:
        for key in ("question", "allowed", "on_timeout", "timeout_minutes", "after"):
            if key not in g:
                bad(w, f"review gate {g.get('id')}: missing {key}")
        if g.get("on_timeout") != "REJECT":
            bad(w, f"review gate {g.get('id')}: on_timeout must be REJECT (fail closed)")
        if g.get("after") not in stage_index:
            bad(w, f"review gate {g.get('id')}: 'after' must be a stage")
        t = g.get("timeout_minutes")
        if isinstance(t, str) and t.startswith("$") and t[1:] not in params:
            bad(w, f"review gate {g.get('id')}: undefined {t}")
    emit = d.get("decision_policy", {}).get("emit_signal_when")
    if emit == "review_gates_passed":
        if not gates:
            bad(w, "emit_signal_when=review_gates_passed but no review gates")
    elif emit not in stage_index:
        bad(w, f"decision_policy.emit_signal_when {emit!r} must be a stage or review_gates_passed")
    if mode in {"DETERMINISTIC", "HYBRID"}:
        for key in ("entry", "stop", "target", "exits"):
            if not d.get(key):
                bad(w, f"signal-emitting definition must declare {key}")
    return d


def check_parameter_sets(path: Path, definitions: dict) -> None:
    ps = yaml.safe_load(path.read_text())
    w = path.name
    d = definitions.get(ps.get("definition"))
    if d is None:
        bad(w, f"definition {ps.get('definition')!r} not found among examples")
        return
    seen = set()
    for s in ps.get("sets", []):
        if s["id"] in seen:
            bad(w, f"duplicate set id {s['id']}")
        seen.add(s["id"])
        for name, value in s.get("values", {}).items():
            spec = d["parameters"].get(name)
            if spec is None:
                bad(w, f"{s['id']}: undeclared parameter {name}")
                continue
            lo, hi = spec["bounds"]
            if not (lo <= value <= hi):
                bad(w, f"{s['id']}: {name}={value} outside [{lo},{hi}]")


def main() -> int:
    catalog = yaml.safe_load((EX / "primitive_catalog_v1_seed.yaml").read_text())["primitives"]
    definitions = {}
    for path in sorted(EX.glob("*.definition.yaml")):
        d = check_definition(path, catalog)
        definitions[d["strategy_key"]] = d
    for path in sorted(EX.glob("*.parameter_sets.yaml")):
        check_parameter_sets(path, definitions)
    if findings:
        print(f"{len(findings)} finding(s):")
        print("\n".join(" - " + f for f in findings))
        return 1
    print(f"OK: {len(definitions)} definition(s), catalogue of {len(catalog)} primitive(s), no findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
