"""Phase 5C target-lifecycle audit after the Phase 5B geometry correction."""
from __future__ import annotations

import csv
import json
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from research.context_structure_retrace_phase3.phase3 import _outcome
from research.context_structure_retrace_phase4.phase4 import (
    DATA_DIR, DATA_FILES, _flatten, _metric, _percentile, _read_rows,
)
from .phase5 import _bucket, _geometry

ROOT = Path(__file__).resolve().parent


def _target_reached_before_fill(x: dict[str, Any], bars: list[dict[str, Any]], target: float | None) -> bool:
    if target is None or x.get("fill_time") is None:
        return False
    fill_index = int(x["lower_index"])
    setup_time = int(x["setup_timestamp"])
    times = [int(bar["time"]) for bar in bars]
    start = bisect_right(times, setup_time)
    for bar in bars[start:fill_index]:
        if x["direction"] == "LONG" and float(bar["high"]) >= float(target):
            return True
        if x["direction"] == "SHORT" and float(bar["low"]) <= float(target):
            return True
    return False


def fill_index_to_time(bars: list[dict[str, Any]], index: int) -> int:
    return int(bars[index]["time"]) if 0 <= index < len(bars) else 0


def _classify(x: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    extension = x["target_hypotheses"].get("CANDLE_EXTENSION_50")
    entry = float(x["entry_price"])
    theoretical = float(x["entry_reference"])
    direction = x["direction"]
    if extension is None:
        extension_state = "UNAVAILABLE"
        extension_valid = False
    elif direction == "LONG":
        extension_state = "TARGET_BEYOND_ENTRY" if extension > entry else "TARGET_AT_ENTRY" if extension == entry else "TARGET_BEHIND_ENTRY"
        extension_valid = extension > entry
    else:
        extension_state = "TARGET_BEYOND_ENTRY" if extension < entry else "TARGET_AT_ENTRY" if extension == entry else "TARGET_BEHIND_ENTRY"
        extension_valid = extension < entry
    reached = _target_reached_before_fill(x, bars, extension)
    reentry = int(x.get("entry_opportunity_number", 1)) > 1 or bool(x.get("potential_scale_in"))
    if not extension_valid:
        theoretical_valid = (extension > theoretical) if direction == "LONG" else (extension < theoretical)
        spread_crossed = theoretical_valid and ((extension <= entry) if direction == "LONG" else (extension >= entry))
        if reentry and reached:
            cause = "REENTRY_AFTER_TARGET_WAS_PREVIOUSLY_REACHED"
        elif spread_crossed:
            cause = "ENTRY_PRICE_SPREAD_MOVED_BEYOND_TARGET"
        elif reached:
            cause = "ENTRY_OCCURRED_AFTER_EXTENSION_TARGET"
        else:
            cause = "ENTRY_OCCURRED_AFTER_EXTENSION_TARGET"
    else:
        cause = None
    if reentry:
        lifecycle = "RETURN_AFTER_SETUP_TARGET_COMPLETED" if reached else "REENTRY_BEFORE_TARGET_COMPLETION"
    else:
        lifecycle = "NOT_REENTRY"
    return {"extension_state": extension_state, "extension_valid": extension_valid, "extension_reached_before_fill": reached, "cause": cause, "reentry_lifecycle": lifecycle, "signed_extension_distance": (extension - entry) if direction == "LONG" else (entry - extension) if extension is not None else None}


def _corrected_outcome(x: dict[str, Any], data: dict[str, Any], geometry: dict[str, Any], classification: dict[str, Any], m1: dict | None = None) -> dict[str, Any]:
    if geometry["target_state"] != "TARGET_BEYOND_ENTRY":
        return {"outcome": "NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY", "r": None}
    stop = float(x["stop_hypotheses"]["ORIGINATING_SETUP_EXTREME"]["stop"])
    target = float(geometry["effective_target"])
    candidate = {"lower_index": int(x["lower_index"]), "entry_price": float(x["entry_price"])}
    return _outcome(data["M5"], candidate, stop, target, x["direction"], m1=m1)


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {"p10": _percentile(values, .1), "p25": _percentile(values, .25), "median": _percentile(values, .5), "p75": _percentile(values, .75), "p90": _percentile(values, .9)}


def audit_symbol(symbol: str, rows: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    _, normalized = _flatten(symbol, rows, data)
    positions = normalized["economic_positions"]
    records = []
    m1 = None
    if data.get("M1"):
        m1 = defaultdict(list)
        for b in data["M1"]:
            m1[(int(b["time"]) // 300) * 300].append(b)
        m1 = dict(m1)
    for x in positions:
        geometry = _geometry(x)
        classification = _classify(x, data["M5"])
        outcome = _corrected_outcome(x, data, geometry, classification, m1)
        records.append({**x, "geometry": geometry, "classification": classification, "corrected_outcome": outcome})
    valid = [x for x in records if x["geometry"]["target_state"] == "TARGET_BEYOND_ENTRY" and x["corrected_outcome"].get("r") is not None]
    behind = [x for x in records if x["geometry"]["target_state"] == "TARGET_BEHIND_ENTRY"]
    at = [x for x in records if x["geometry"]["target_state"] == "TARGET_AT_ENTRY"]
    causes = Counter(x["classification"]["cause"] for x in behind)
    by_mechanism = defaultdict(Counter)
    for x in behind:
        mechanisms = x.get("signal_evidence") or ["UNSPECIFIED"]
        for mechanism in mechanisms:
            by_mechanism[mechanism][x["classification"]["cause"]] += 1
    lifecycle = Counter(x["classification"]["reentry_lifecycle"] for x in records)
    lifecycle_metrics = {}
    for name in ("REENTRY_BEFORE_TARGET_COMPLETION", "RETURN_AFTER_SETUP_TARGET_COMPLETED"):
        selected = [x for x in valid if x["classification"]["reentry_lifecycle"] == name]
        lifecycle_metrics[name] = {"count": len(selected), "metrics": _metric([{**x, "baseline_outcome": x["corrected_outcome"]} for x in selected])}
    target_rs = [float(x["geometry"]["target_r"]) for x in valid if x["geometry"]["target_r"] is not None]
    buckets = Counter(_bucket(r) for r in target_rs)
    return {
        "counts": {"all_economic_positions": len(records), "directionally_valid_target_positions": len(valid), "target_behind_entry": len(behind), "target_at_entry": len(at), "reentry_before_target_completion": lifecycle["REENTRY_BEFORE_TARGET_COMPLETION"], "return_after_setup_target_completed": lifecycle["RETURN_AFTER_SETUP_TARGET_COMPLETED"]},
        "root_causes": dict(causes),
        "root_causes_by_entry_mechanism": {k: dict(v) for k, v in by_mechanism.items()},
        "reentry_lifecycle": dict(lifecycle),
        "reentry_lifecycle_metrics": lifecycle_metrics,
        "corrected_descriptive_metrics": {"all_economic_positions": _metric([{**x, "baseline_outcome": x["corrected_outcome"]} for x in records]), "directionally_valid_target_positions": _metric([{**x, "baseline_outcome": x["corrected_outcome"]} for x in valid])},
        "target_R_distribution_signed_valid": _distribution(target_rs),
        "target_R_buckets_signed_valid": dict(buckets),
        "records": records,
    }


def write_outputs(reports: dict[str, Any]) -> None:
    result = {"schema": "context_structure_retrace_phase5c_target_lifecycle", "phase": "5C", "research_only": True, "strategy_frozen": False, "paper_runner_started": False, "prior_phase5_metrics": "SUPERSEDED_BY_PHASE5C_CORRECTED_GEOMETRY", "instruments": {}}
    fields = ["symbol", "economic_position_id", "setup_id", "direction", "pattern", "fill_timestamp", "entry_price", "extension_target", "opposing_structure", "effective_target", "target_state", "signed_target_distance", "target_r", "cause", "reentry_lifecycle", "outcome", "r"]
    with (ROOT / "context_structure_retrace_phase5c_positions.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for symbol, report in reports.items():
            result["instruments"][symbol] = {k: v for k, v in report.items() if k != "records"}
            for x in report["records"]:
                g, c, o = x["geometry"], x["classification"], x["corrected_outcome"]
                writer.writerow({"symbol": symbol, "economic_position_id": x["economic_position_id"], "setup_id": x["setup_id"], "direction": x["direction"], "pattern": x["pattern"], "fill_timestamp": x.get("fill_timestamp"), "entry_price": x["entry_price"], "extension_target": x["target_hypotheses"].get("CANDLE_EXTENSION_50"), "opposing_structure": x["target_hypotheses"].get("NEXT_OPPOSING_STRUCTURE"), "effective_target": g.get("effective_target"), "target_state": g.get("target_state"), "signed_target_distance": g.get("target_distance"), "target_r": g.get("target_r"), "cause": c.get("cause"), "reentry_lifecycle": c.get("reentry_lifecycle"), "outcome": o.get("outcome"), "r": o.get("r")})
    (ROOT / "context_structure_retrace_phase5c_results.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    lines = ["# CONTEXT_STRUCTURE_RETRACE_V1 — Phase 5C target-lifecycle audit", "", "The Phase 4/5 metrics are SUPERSEDED because they included target geometry that could select a structure behind the executable entry and then use absolute distance.", "", "## Corrected lifecycle", "", "The setup extension remains attached to the originating setup. It is not recalculated for later entries. Directionally invalid targets are represented as `NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY`, not as positive R.", "", "| Instrument | Economic positions | Valid target | Behind entry | At entry | Re-entry before target | Return after target | Corrected PF | Corrected expectancy R |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for symbol, r in reports.items():
        m = r["corrected_descriptive_metrics"]["directionally_valid_target_positions"]
        c = r["counts"]
        lines.append(f"| {symbol} | {c['all_economic_positions']} | {c['directionally_valid_target_positions']} | {c['target_behind_entry']} | {c['target_at_entry']} | {c['reentry_before_target_completion']} | {c['return_after_setup_target_completed']} | {m['profit_factor'] if m['profit_factor'] is not None else 'n/a'} | {m['expectancy_r'] if m['expectancy_r'] is not None else 'n/a'} |")
    lines += ["", "## Root causes", ""]
    for symbol, r in reports.items():
        lines.append(f"- {symbol}: {json.dumps(r['root_causes'], sort_keys=True)}")
        lines.append(f"  - By entry mechanism: {json.dumps(r['root_causes_by_entry_mechanism'], sort_keys=True)}")
    lines += ["", "## Re-entry semantics", "", "`REENTRY_BEFORE_TARGET_COMPLETION` and `RETURN_AFTER_SETUP_TARGET_COMPLETED` are persisted separately. The latter is not automatically a valid V1 re-entry and requires a new approved setup interpretation.", "", "## Target distributions", ""]
    for symbol, r in reports.items():
        lines.append(f"- {symbol}: distribution={json.dumps(r['target_R_distribution_signed_valid'], sort_keys=True)}; buckets={json.dumps(r['target_R_buckets_signed_valid'], sort_keys=True)}")
    lines += ["", "## Remaining decisions", "", "1. Decide whether a return after the originating setup target completed requires a new setup event.", "2. Decide whether target-behind/at-entry cases are review-only or structurally non-tradable.", "3. Approve target lifecycle and re-entry semantics before freeze.", "", "No optimization, freeze, or forward collection was performed."]
    (ROOT / "context_structure_retrace_phase5c_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    reports = {}
    for symbol, rows in _read_rows().items():
        data = json.loads((DATA_DIR / DATA_FILES[symbol]).read_text(encoding="utf-8"))
        reports[symbol] = audit_symbol(symbol, rows, data)
    write_outputs(reports)
    print(json.dumps({s: {"counts": r["counts"], "causes": r["root_causes"], "metrics": r["corrected_descriptive_metrics"]["directionally_valid_target_positions"]} for s, r in reports.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
