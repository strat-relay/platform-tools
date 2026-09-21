"""Phase 5 structural-room and freeze-candidate study.

Consumes Phase 3/4 research artifacts only.  No trade selection is frozen,
no runner is started, and no outcome is used to choose a room threshold.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from research.context_structure_retrace_phase4.phase4 import (
    CANONICAL_STOP,
    DATA_DIR,
    DATA_FILES,
    PHASE3,
    _flatten,
    _metric,
    _percentile,
    _read_rows,
    _sequential,
    _slippage_view,
)

ROOT = Path(__file__).resolve().parent


ROOM_BUCKETS = (
    ("<0.10R", float("-inf"), 0.10),
    ("0.10-0.25R", 0.10, 0.25),
    ("0.25-0.50R", 0.25, 0.50),
    ("0.50-0.75R", 0.50, 0.75),
    ("0.75-1.00R", 0.75, 1.00),
    ("1.00-1.50R", 1.00, 1.50),
    (">1.50R", 1.50, float("inf")),
)


def _bucket(value: float) -> str:
    for name, low, high in ROOM_BUCKETS:
        if low <= value < high:
            return name
    return ROOM_BUCKETS[-1][0]


def _geometry(x: dict[str, Any]) -> dict[str, Any]:
    stop = x["stop_hypotheses"][CANONICAL_STOP]
    entry = float(x["entry_price"])
    direction = x["direction"]
    extension = x["target_hypotheses"].get("CANDLE_EXTENSION_50")
    opposing = x["target_hypotheses"].get("NEXT_OPPOSING_STRUCTURE")
    if opposing is None:
        opposing_state = "UNAVAILABLE"
        opposing_valid = False
    elif direction == "LONG":
        opposing_state = "TARGET_BEYOND_ENTRY" if opposing > entry else "TARGET_AT_ENTRY" if opposing == entry else "TARGET_BEHIND_ENTRY"
        opposing_valid = opposing > entry
    else:
        opposing_state = "TARGET_BEYOND_ENTRY" if opposing < entry else "TARGET_AT_ENTRY" if opposing == entry else "TARGET_BEHIND_ENTRY"
        opposing_valid = opposing < entry
    # A structure behind the executable entry cannot be a profit target. Keep
    # it as an observed structure/state, but do not let it cap the target.
    candidates = [extension] if extension is not None else []
    if opposing_valid:
        candidates.append(opposing)
    if direction == "LONG":
        effective = min(candidates) if candidates else extension
    else:
        effective = max(candidates) if candidates else extension
    risk = float(stop["stop_distance"])
    stop_atr = float(stop.get("stop_distance_atr") or 0.0)
    atr_value = risk / stop_atr if stop_atr else None
    target_distance = ((float(effective) - entry) if direction == "LONG" else (entry - float(effective))) if effective is not None else None
    target_r = target_distance / risk if target_distance is not None and risk > 0 else None
    target_atr = target_distance / atr_value if target_distance is not None and atr_value else None
    spread = float(x["spread"])
    spread_target = spread / abs(target_distance) if target_distance else None
    spread_stop = spread / risk if risk else None
    flags = []
    if target_r is not None and target_r <= 0:
        flags.append("TARGET_BEHIND_OR_AT_ENTRY")
    if target_r is not None and target_r < 0.10:
        flags.append("OPPOSING_STRUCTURE_VERY_CLOSE")
    if target_r is not None and target_r < 0.25:
        flags.append("LOW_REWARD_TO_STRUCTURAL_RISK")
    if spread_target is not None and spread_target >= 0.25:
        flags.append("TARGET_NEAR_SPREAD_SCALE")
    return {
        "entry": entry,
        "stop": float(stop["stop"]),
        "stop_distance": risk,
        "stop_distance_atr": stop_atr,
        "atr": atr_value,
        "extension_target": extension,
        "opposing_structure": opposing,
        "opposing_structure_state": opposing_state,
        "opposing_structure_valid_profit_target": opposing_valid,
        "effective_target": effective,
        "target_distance": target_distance,
        "target_r": target_r,
        "target_atr": target_atr,
        "spread": spread,
        "spread_to_target": spread_target,
        "spread_to_stop": spread_stop,
        "target_state": "TARGET_BEYOND_ENTRY" if target_r is not None and target_r > 0 else "TARGET_AT_ENTRY" if target_r == 0 else "TARGET_BEHIND_ENTRY" if target_r is not None else "UNAVAILABLE",
        "room_bucket": _bucket(target_r) if target_r is not None and target_r >= 0 else "TARGET_BEHIND_ENTRY",
        "flags": flags,
    }


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {"p10": _percentile(values, .10), "p25": _percentile(values, .25), "median": _percentile(values, .50), "p75": _percentile(values, .75), "p90": _percentile(values, .90)}


def _review_case(x: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    # Deliberately omit all future outcome fields.
    return {
        "symbol": x["symbol"],
        "setup_id": x["setup_id"],
        "timestamp": x["setup_timestamp"],
        "review_timestamp": x.get("fill_time"),
        "direction": x["direction"],
        "pattern": x["pattern"],
        "signal_evidence": x["signal_evidence"],
        "entry": g["entry"],
        "originating_stop": g["stop"],
        "extension_target": g["extension_target"],
        "opposing_structure": g["opposing_structure"],
        "effective_capped_target": g["effective_target"],
        "target_r": g["target_r"],
        "target_state": g["target_state"],
        "target_atr": g["target_atr"],
        "spread": g["spread"],
        "spread_to_target": g["spread_to_target"],
        "structural_flags": g["flags"],
        "context_flags": x["qualification_flags"],
        "context": x["context_components"],
        "future_outcome_attached": False,
    }


def audit_symbol(symbol: str, rows: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    _, normalized = _flatten(symbol, rows, data)
    positions = [x for x in normalized["economic_positions"] if x.get("fill_time") is not None]
    enriched = []
    for x in positions:
        y = dict(x)
        y["geometry"] = _geometry(x)
        enriched.append(y)
    room_values = [float(x["geometry"]["target_r"]) for x in enriched if x["geometry"]["target_r"] is not None]
    atr_values = [float(x["geometry"]["target_atr"]) for x in enriched if x["geometry"]["target_atr"] is not None]
    spread_target = [float(x["geometry"]["spread_to_target"]) for x in enriched if x["geometry"]["spread_to_target"] is not None]
    spread_stop = [float(x["geometry"]["spread_to_stop"]) for x in enriched if x["geometry"]["spread_to_stop"] is not None]
    room_counts = Counter(x["geometry"]["room_bucket"] for x in enriched)
    flags = Counter(f for x in enriched for f in x["geometry"]["flags"])
    room_views = {}
    for name, _, _ in ROOM_BUCKETS:
        selected = [x for x in enriched if x["geometry"]["room_bucket"] == name]
        # These are descriptive outcome views only; no bucket is selected.
        room_views[name] = {"count": len(selected), "descriptive_outcome": _metric(selected) if selected else None}
    review_cases = []
    for name, _, _ in ROOM_BUCKETS:
        selected = sorted((x for x in enriched if x["geometry"]["room_bucket"] == name), key=lambda z: (z["setup_timestamp"], z["setup_id"]))
        review_cases.extend(_review_case(x, x["geometry"]) for x in selected[:3])
    seq = _sequential(enriched, "ONE_POSITION_AT_A_TIME")
    # Sequential baseline is intentionally conservative while re-entry remains
    # unresolved: one economic position at a time and no scale-in.
    viability_counts = {
        "structurally_valid": len(enriched),
        "flagged_very_close": flags.get("OPPOSING_STRUCTURE_VERY_CLOSE", 0),
        "flagged_low_reward": flags.get("LOW_REWARD_TO_STRUCTURAL_RISK", 0),
        "flagged_target_near_spread": flags.get("TARGET_NEAR_SPREAD_SCALE", 0),
        "structurally_valid_but_economically_flagged": sum(bool(x["geometry"]["flags"]) for x in enriched),
        "hard_viability_rejections": 0,
    }
    return {
        "counts": {"economic_positions": len(enriched), "sequential_baseline_positions": seq["accepted"], "sequential_overlap_rejected": seq["rejected_overlap"]},
        "structural_validity_vs_trade_viability": viability_counts,
        "room_distribution_R": _distribution(room_values),
        "target_ATR_distribution": _distribution(atr_values),
        "spread_to_target_distribution": _distribution(spread_target),
        "spread_to_stop_distribution": _distribution(spread_stop),
        "room_buckets": room_views,
        "flag_counts": dict(flags),
        "sequential_baseline": seq,
        "cost_model": {
            "observed_spread_only": _metric(enriched),
            "spread_plus_small_slippage_0_5x": _slippage_view(enriched, .5),
            "spread_plus_moderate_slippage_1x": _slippage_view(enriched, 1.0),
            "commission": "UNAVAILABLE_IN_HISTORICAL_FIXTURE; sensitivity not invented",
            "broker_metadata": data.get("contract", {}),
        },
        "two_position_risk_definition": {"total_setup_risk_R": 1.0, "leg_a_risk_R": 0.5, "leg_b_risk_R": 0.5, "leg_a": "effective target; target policy unresolved", "leg_b": "runner after Leg A; exit policy UNRESOLVED", "scale_in": "DISABLED"},
        "review_cases": review_cases,
        "positions": enriched,
    }


def write_outputs(reports: dict[str, Any]) -> None:
    result = {
        "schema": "context_structure_retrace_phase5_freeze_candidate",
        "phase": 5,
        "research_only": True,
        "paper_runner_started": False,
        "strategy_frozen": False,
        "development_status": "March-September 2026 EXPOSED DEVELOPMENT DATA; not untouched validation",
        "instruments": {},
        "freeze_candidate": {
            "name": "CONTEXT_STRUCTURE_RETRACE_V1_FREEZE_CANDIDATE",
            "setup_events": ["BULLISH_ENGULFING", "BEARISH_ENGULFING", "MORNING_STAR", "EVENING_STAR", "REJECTION_WICK"],
            "context_representation": "Phase 2 causal S/R attention, trendline/channel candidates, EMA20/50/100/200, configurable M5/M15/H1/H4 context",
            "qualification": "context components and flags recorded; hard context filters UNRESOLVED",
            "structural_validity": "pattern event with causal context and intact originating thesis",
            "trade_viability": "UNRESOLVED; structural-room flags are descriptive only",
            "retracement": "Phase 3 depth/confirmation observations preserved; final entry mechanism UNRESOLVED",
            "entry": "UNRESOLVED between depth-only and confirmation variants",
            "invalidation": "originating setup extreme break before entry",
            "stop": "ORIGINATING_SETUP_EXTREME APPROVED",
            "target": "UNRESOLVED between extension-only and opposing-structure-capped target",
            "re_entry": "UNRESOLVED pending departure definition",
            "scale_in": "DISABLED",
            "risk": "UNRESOLVED account/risk policy; economic position identity required",
            "two_position_management": "total setup risk 1R proposed, 0.5R per leg; runner exit UNRESOLVED",
            "cost_model": "observed spread plus explicit slippage sensitivity; commission UNRESOLVED",
            "timeframe_configuration": {"execution": "M15", "lower": ["M5"], "higher": ["H1", "H4"]},
            "instrument_configuration": "supplied symbol and broker metadata; no symbol-specific strategy branches",
        },
    }
    fields = ["symbol", "setup_id", "timestamp", "direction", "pattern", "entry", "originating_stop", "extension_target", "opposing_structure", "effective_capped_target", "target_r", "target_atr", "spread", "spread_to_target", "future_outcome_attached"]
    with (ROOT / "context_structure_retrace_phase5_review_cases.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for report in reports.values():
            for case in report["review_cases"]:
                w.writerow({k: case.get(k) for k in fields})
    for symbol, report in reports.items():
        result["instruments"][symbol] = {k: v for k, v in report.items() if k not in {"positions"}}
    (ROOT / "context_structure_retrace_phase5_results.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    lines = ["# CONTEXT_STRUCTURE_RETRACE_V1 — Phase 5 freeze-candidate study", "", "Read-only structural-room and manual-definition study. No runner, orders, optimization, or automatic freeze.", "", "## Approved", "", "- Canonical stop: `ORIGINATING_SETUP_EXTREME`.", "- No scale-in.", "- Total two-position setup risk proposed as 1R, split 0.5R / 0.5R; runner exit remains unresolved.", "", "## Not approved", "", "- Target policy.", "- Minimum structural room / trade-viability threshold.", "- Entry signal variant.", "- Departure/re-entry definition.", "- Commission/slippage policy.", "", "## Structural-room distributions", "", "| Instrument | Positions | Median target R | P25 target R | P10 target R | Median target ATR | Median spread/target | Flagged low-reward |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for symbol, r in reports.items():
        d = r["room_distribution_R"]; lines.append(f"| {symbol} | {r['counts']['economic_positions']} | {d['median']:.3f} | {d['p25']:.3f} | {d['p10']:.3f} | {r['target_ATR_distribution']['median']:.3f} | {r['spread_to_target_distribution']['median']:.3f} | {r['structural_validity_vs_trade_viability']['flagged_low_reward']} |")
    lines += ["", "## Structural validity vs trade viability", "", "All positions remain structurally valid in this study because no room cutoff was approved. Economically questionable geometry is represented as flags, not rejections. The <0.25R cases therefore remain in the ledger for human review."]
    for symbol, r in reports.items():
        v = r["structural_validity_vs_trade_viability"]; lines.append(f"- {symbol}: structural={v['structurally_valid']}; flagged very-close={v['flagged_very_close']}; flagged low-reward={v['flagged_low_reward']}; target-near-spread={v['flagged_target_near_spread']}; hard rejections=0.")
    lines += ["", "## Sequential baseline", "", "The temporary descriptive baseline is one economic position at a time, no scale-in, and no final re-entry policy. It is not frozen for forward testing."]
    for symbol, r in reports.items():
        s = r["sequential_baseline"]; m = s["metrics"]; lines.append(f"- {symbol}: {s['accepted']} accepted, {s['rejected_overlap']} overlap-rejected, expectancy={m['expectancy_r']:.4f}R, PF={m['profit_factor']:.3f}.")
    lines += ["", "## Re-entry proposal for approval", "", "Proposed causal definition: after an entry interaction, require a completed lower-timeframe candle to make a favorable excursion of at least 2 ATR from the entry, at least one full candle outside the entry zone, and then a later completed candle to return to the entry zone. This is a structural interpretation of departure/return, not a profitability-selected threshold. It remains UNRESOLVED.", "", "## Cost findings", "", "The fixtures provide observed spread and broker contract metadata, but no commission/account-type field. Commission remains unresolved. Spread-only and two explicit slippage sensitivities are persisted; no cost was invented.", "", "## Human review cases", "", "Review cases are written without outcome labels in `context_structure_retrace_phase5_review_cases.csv`. They include the entry, originating stop, extension target, opposing structure, capped target, target R, target ATR, spread ratio, and causal context.", "", "## What Phase 6 would do after approval", "", "Freeze the approved configuration/hash, create a prospective paper-only runner for the supplied symbol/timeframe configuration, persist every lifecycle event, and evaluate only post-freeze data as holdout evidence. No runner is created by Phase 5.", "", "## Decisions required", "", "1. Approve or revise the target policy.", "2. Decide whether economically tiny targets are merely flagged or excluded.", "3. Approve one entry/retracement mechanism.", "4. Approve the departure/re-entry definition.", "5. Approve cost/slippage treatment and whether two-leg risk is 0.5R/0.5R.", "6. Approve time-exit and runner policy."]
    (ROOT / "context_structure_retrace_phase5_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    raw = _read_rows()
    reports = {}
    for symbol, rows in raw.items():
        data = json.loads((DATA_DIR / DATA_FILES[symbol]).read_text())
        reports[symbol] = audit_symbol(symbol, rows, data)
    write_outputs(reports)
    print(json.dumps({s: {"counts": r["counts"], "room": r["room_distribution_R"], "sequential": r["sequential_baseline"]["accepted"]} for s, r in reports.items()}, indent=2))


if __name__ == "__main__":
    main()
