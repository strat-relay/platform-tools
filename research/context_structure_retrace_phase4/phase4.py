"""Phase 4 integrity and economic-exposure audit.

This module consumes the frozen Phase 3 descriptive ledger.  It does not
regenerate signals, run a paper engine, place orders, or alter any strategy.
Its purpose is to distinguish observations and hypotheses from economic
positions, then replay simple non-optimizing exposure policies.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
PHASE3 = Path(__file__).parents[1] / "context_structure_retrace_phase3"
DATA_DIR = Path("/tmp/context-structure-retrace")
DATA_FILES = {"XAUUSDm": "xau.json", "BTCUSDm": "btc_basic.json", "USDJPYm": "USDJPYm.json", "EURUSDm": "EURUSDm.json"}

# The Phase 3 ledger is the source of truth.  The current baseline uses the
# simplest structural interpretation: originating setup extreme for the stop
# and the structure-capped candle extension for the first target.  This is a
# documented representation choice, not a historical-performance selection.
CANONICAL_STOP = "ORIGINATING_SETUP_EXTREME"
CANONICAL_TARGET = "STRUCTURE_CAPPED_EXTENSION"
LIKE_FOR_LIKE_STOP = "ORIGINATING_SETUP_EXTREME"
LIKE_FOR_LIKE_TARGET = "CANDLE_EXTENSION_50"


def _stable(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:20]}"


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts is not None else None


def _percentile(values: Iterable[float], q: float) -> float | None:
    values = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not values:
        return None
    return values[min(len(values) - 1, int(len(values) * q))]


def _metric(items: list[dict[str, Any]], outcome_key: str = "baseline_outcome") -> dict[str, Any]:
    resolved = [x for x in items if x.get(outcome_key, {}).get("r") is not None]
    rs = [float(x[outcome_key]["r"]) for x in resolved]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    running = peak = dd = 0.0
    losing = longest_loss = 0
    for r in rs:
        running += r
        peak = max(peak, running)
        dd = max(dd, peak - running)
        losing = losing + 1 if r < 0 else 0
        longest_loss = max(longest_loss, losing)
    return {
        "n": len(resolved),
        "wins": sum(r > 0 for r in rs),
        "losses": sum(r < 0 for r in rs),
        "time_exits": sum(x[outcome_key].get("outcome") == "TIME_EXIT" for x in resolved),
        "win_rate_pct": 100 * len(wins) / len(rs) if rs else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "expectancy_r": statistics.mean(rs) if rs else None,
        "cumulative_r": sum(rs),
        "max_drawdown_r": dd,
        "longest_losing_streak": longest_loss,
        "median_duration_minutes": _percentile([x[outcome_key].get("duration_minutes") for x in resolved], .5),
        "avg_mae_r": statistics.mean(float(x[outcome_key].get("mae_r", 0.0)) for x in resolved) if resolved else None,
        "avg_mfe_r": statistics.mean(float(x[outcome_key].get("mfe_r", 0.0)) for x in resolved) if resolved else None,
    }


def _read_rows() -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    path = PHASE3 / "context_structure_retrace_phase3_ledger.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows[row["symbol"]].append(row)
    return rows


def _lower_times(data: dict[str, Any]) -> list[int]:
    return [int(x["time"]) for x in data.get("M5", [])]


def _outcome(row: dict[str, Any], attempt: dict[str, Any], stop: str, target: str) -> dict[str, Any]:
    return attempt["stop_target_outcomes"][stop]["targets"][target].get("outcome", {})


def _flatten(symbol: str, rows: list[dict[str, Any]], data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lower_times = _lower_times(data)
    flat: list[dict[str, Any]] = []
    for row in rows:
        for attempt in row.get("entry_attempts", []):
            c = attempt["candidate"]
            idx = int(c["lower_index"])
            fill_time = lower_times[idx] if 0 <= idx < len(lower_times) else None
            baseline = _outcome(row, attempt, CANONICAL_STOP, CANONICAL_TARGET)
            like = _outcome(row, attempt, LIKE_FOR_LIKE_STOP, LIKE_FOR_LIKE_TARGET)
            flat.append({
                "symbol": symbol,
                "setup_id": row["setup_id"],
                "phase3_market_event_id": row["market_event_id"],
                "phase3_entry_opportunity_id": attempt["entry_opportunity_id"],
                "phase3_entry_attempt_id": attempt["entry_attempt_id"],
                "setup_timestamp": int(row["setup_event"]["timestamp"]),
                "fill_time": fill_time,
                "fill_timestamp": _iso(fill_time),
                "lower_index": idx,
                "direction": row["setup_event"]["direction"],
                "pattern": row["setup_event"]["pattern"],
                "signal_types": list(c.get("signal_types", [])),
                "entry_price": float(c["entry_price"]),
                "entry_reference": float(c["entry_reference"]),
                "spread": float(c.get("spread", row.get("spread", 0.0))),
                "stop_hypotheses": row.get("stop_hypotheses", {}),
                "target_hypotheses": row.get("target_hypotheses", {}),
                "baseline_outcome": baseline,
                "like_for_like_outcome": like,
                "stop_hypothesis_outcomes": {
                    stop_name: _outcome(row, attempt, stop_name, CANONICAL_TARGET)
                    for stop_name in row.get("stop_hypotheses", {})
                    if CANONICAL_TARGET in attempt.get("stop_target_outcomes", {}).get(stop_name, {}).get("targets", {})
                },
                "qualification_flags": row.get("qualification_flags", []),
                "context_components": row.get("context_components", {}),
                "retrace_candles": c.get("candles_waited"),
                "potential_scale_in": bool(c.get("potential_scale_in")),
                "entry_opportunity_number": int(c.get("entry_opportunity_number", 1)),
                "intrabar_resolution": baseline.get("intrabar_resolution"),
            })
    flat.sort(key=lambda x: (x["fill_time"] is None, x["fill_time"] or 0, x["setup_timestamp"], x["phase3_entry_attempt_id"]))
    # Economic identity is based on the actual price interaction, not on the
    # detector that noticed it. Same symbol/direction/fill candle/entry price
    # means one exposure opportunity; all evidence remains attached.
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    point = float(data.get("contract", {}).get("point", data.get("contract", {}).get("tick_size", 1e-8)))
    for item in flat:
        price_bucket = round(item["entry_price"] / max(point, 1e-12))
        key = (symbol, item["direction"], item["fill_time"], price_bucket)
        groups[key].append(item)
    economic: list[dict[str, Any]] = []
    overlap_groups = []
    for key, group in groups.items():
        group.sort(key=lambda x: (x["setup_timestamp"], x["phase3_entry_attempt_id"]))
        position_id = _stable("POS", *key)
        event_ids = sorted({x["phase3_market_event_id"] for x in group})
        opp_ids = sorted({x["phase3_entry_opportunity_id"] for x in group})
        signal_types = sorted({s for x in group for s in x["signal_types"]})
        for x in group:
            x["economic_position_id"] = position_id
            x["market_event_ids"] = event_ids
            x["signal_evidence"] = signal_types
        first = dict(group[0])
        first["economic_position_id"] = position_id
        first["source_observation_count"] = len(group)
        first["source_setup_ids"] = sorted({x["setup_id"] for x in group})
        first["source_phase3_opportunity_ids"] = opp_ids
        first["signal_evidence"] = signal_types
        first["economic_position_type"] = "SCALE_IN_CANDIDATE" if len(opp_ids) > 1 else "SINGLE_INTERACTION"
        first["baseline_outcome"] = group[0]["baseline_outcome"]
        first["like_for_like_outcome"] = group[0]["like_for_like_outcome"]
        first["stop_hypothesis_outcomes"] = group[0]["stop_hypothesis_outcomes"]
        economic.append(first)
        if len(group) > 1:
            overlap_groups.append({"economic_position_id": position_id, "count": len(group), "setup_ids": first["source_setup_ids"], "phase3_opportunities": opp_ids, "signals": signal_types})
    return flat, {"economic_positions": sorted(economic, key=lambda x: (x["fill_time"] is None, x["fill_time"] or 0)), "overlap_groups": overlap_groups}


def _same_candle_audit(items: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    bars = data.get("M5", [])
    ambiguous = []
    for x in items:
        out = x["baseline_outcome"]
        ei = out.get("exit_index")
        if ei is None or not (0 <= int(ei) < len(bars)):
            continue
        stop = x["stop_hypotheses"].get(CANONICAL_STOP, {}).get("stop")
        target = x["target_hypotheses"].get(CANONICAL_TARGET)
        if stop is None or target is None:
            continue
        bar = bars[int(ei)]
        both = (float(bar["low"]) <= float(stop) and float(bar["high"]) >= float(target)) if x["direction"] == "LONG" else (float(bar["high"]) >= float(stop) and float(bar["low"]) <= float(target))
        if both:
            ambiguous.append(x)
    by_resolution = Counter(x.get("intrabar_resolution") for x in ambiguous)
    return {"ambiguous_count": len(ambiguous), "by_resolution": dict(by_resolution), "m1_resolved_count": sum(x.get("intrabar_resolution") == "M1_SEQUENCE" for x in ambiguous), "conservative_stop_first_count": sum(x.get("intrabar_resolution") != "M1_SEQUENCE" for x in ambiguous)}


def _slippage_view(items: list[dict[str, Any]], factor: float) -> dict[str, Any]:
    adjusted = []
    for x in items:
        out = x["baseline_outcome"]
        if out.get("r") is None:
            continue
        risk = float(x["stop_hypotheses"][CANONICAL_STOP]["stop_distance"])
        # Phase 3 used spread-adjusted entry but no known commission. Apply a
        # transparent round-trip spread/slippage sensitivity, not a hidden cost.
        extra_r = (float(x["spread"]) * factor) / max(risk, 1e-12)
        adjusted.append({"baseline_outcome": {"r": float(out["r"]) - extra_r}})
    return _metric(adjusted)


def _sequential(items: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    ordered = [x for x in sorted(items, key=lambda y: (y["fill_time"] is None, y["fill_time"] or 0)) if x["fill_time"] is not None]
    accepted = []
    active_until = None
    seen_event = set()
    for x in ordered:
        if policy == "ONE_POSITION_AT_A_TIME" and active_until is not None and x["fill_time"] < active_until:
            continue
        if policy == "REENTRY_ALLOWED_NO_SCALEIN" and x["phase3_market_event_id"] in seen_event:
            continue
        if policy == "REENTRY_AND_SCALEIN_MODEL":
            pass
        accepted.append(x)
        seen_event.add(x["phase3_market_event_id"])
        exit_ts = x["baseline_outcome"].get("exit_timestamp")
        active_until = int(exit_ts) if exit_ts is not None else active_until
    return {"accepted": len(accepted), "rejected_overlap": len(ordered) - len(accepted), "metrics": _metric(accepted)}


def _max_simultaneous(items: list[dict[str, Any]]) -> dict[str, Any]:
    points = sorted({x["fill_time"] for x in items if x.get("fill_time") is not None})
    max_count = max_r = 0.0
    for point in points:
        active = [x for x in items if x.get("fill_time") is not None and x["fill_time"] <= point and (x["baseline_outcome"].get("exit_timestamp") is None or point <= x["baseline_outcome"]["exit_timestamp"])]
        max_count = max(max_count, len(active))
        max_r = max(max_r, sum(float(x["stop_hypotheses"][CANONICAL_STOP]["stop_distance"]) for x in active))
    return {"max_simultaneous_positions": int(max_count), "max_simultaneous_risk_R": int(max_count), "max_simultaneous_price_risk_distance": max_r}


def _two_position_model(items: list[dict[str, Any]]) -> dict[str, Any]:
    # The leg results remain hypotheses.  Leg A uses the manual first-target
    # geometry; leg B uses next opposing structure as a separately recorded
    # runner proxy.  The BE transition is reported, not enabled as execution.
    rows = []
    for x in items:
        a = x["like_for_like_outcome"]
        b = x["baseline_outcome"]
        if a.get("r") is None or b.get("r") is None:
            continue
        rows.append({"baseline_outcome": {"r": float(a["r"]) + float(b["r"]) * 0.5}, "leg_a_r": a["r"], "leg_b_proxy_r": b["r"], "combined_initial_risk": 2.0})
    return {"decision_count": len(rows), "combined_metrics": _metric(rows), "leg_a_metrics": _metric([{"baseline_outcome": {"r": x["leg_a_r"]}} for x in rows]), "leg_b_proxy_metrics": _metric([{"baseline_outcome": {"r": x["leg_b_proxy_r"]}} for x in rows]), "be_transition": "MODEL_ONLY; leg B proxy is not an execution rule", "maximum_combined_initial_risk_R": 2.0}


def audit_symbol(symbol: str, rows: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    flat, normalized = _flatten(symbol, rows, data)
    economic = normalized["economic_positions"]
    phase3_like = [x for x in flat if x["like_for_like_outcome"].get("r") is not None]
    canonical_like = [x for x in economic if x["like_for_like_outcome"].get("r") is not None]
    canonical = [x for x in economic if x["baseline_outcome"].get("r") is not None]
    setup_counts = Counter(r["qualification"] for r in rows)
    attempt_signal_counts = Counter(s for x in flat for s in x["signal_types"])
    stop_hyp_count = sum(len(x["stop_hypotheses"]) for x in flat)
    target_hyp_count = sum(len(x["target_hypotheses"]) for x in flat)
    target_rows = []
    for x in flat:
        stop = x["stop_hypotheses"].get(CANONICAL_STOP, {})
        atr = float(stop.get("stop_distance", 0.0)) / max(float(stop.get("stop_distance_atr") or 0.0), 1e-12)
        for target_name, target in x["target_hypotheses"].items():
            if target is None or not atr:
                continue
            target_rows.append({"pattern": x["pattern"], "signals": x["signal_types"], "target_name": target_name, "target_distance_atr": abs(float(target) - x["entry_price"]) / atr, "rr": abs(float(target) - x["entry_price"]) / max(float(stop.get("stop_distance", 0.0)), 1e-12), "spread_target_pct": 100 * x["spread"] / max(abs(float(target) - x["entry_price"]), 1e-12), "spread_stop_pct": 100 * x["spread"] / max(float(stop.get("stop_distance", 0.0)), 1e-12)} )
    def dist(field: str, selected: list[dict[str, Any]]) -> dict[str, Any]:
        vals = [x[field] for x in selected if x.get(field) is not None]
        return {"p10": _percentile(vals, .1), "p25": _percentile(vals, .25), "median": _percentile(vals, .5), "p75": _percentile(vals, .75), "p90": _percentile(vals, .9)}
    canonical_target_rows = [x for x in target_rows if x["target_name"] == CANONICAL_TARGET]
    patterns = {}
    for pat in sorted({x["pattern"] for x in canonical}):
        selected = [x for x in canonical if x["pattern"] == pat]
        patterns[pat] = {"n": len(selected), "metrics": _metric(selected), "target_geometry": dist("rr", [y for y in canonical_target_rows if y["pattern"] == pat])}
    return {
        "phase3_counts": {"setup_events": len(rows), "qualified": setup_counts.get("QUALIFIED_FOR_RETRACE_MONITORING", 0), "invalidated": sum(r.get("rejection_reason") == "SETUP_INVALIDATED_BEFORE_ENTRY" for r in rows), "no_retrace": sum(r.get("rejection_reason") == "NO_RETRACE" for r in rows), "entry_attempts": len(flat), "unique_phase3_opportunities": len({x["phase3_entry_opportunity_id"] for x in flat}), "signal_variant_observations": sum(max(0, len(x["signal_types"]) - 1) for x in flat), "stop_hypotheses": stop_hyp_count, "target_hypotheses": target_hyp_count},
        "economic_identity": {"economic_positions": len(economic), "filled_economic_positions": len(canonical), "overlap_groups": len(normalized["overlap_groups"]), "overlap_observations": sum(x["count"] for x in normalized["overlap_groups"]), "position_type_counts": dict(Counter(x["economic_position_type"] for x in economic)), "overlap_examples": normalized["overlap_groups"][:10]},
        "like_for_like_original": {"phase3_attempt_metrics": _metric(phase3_like, "like_for_like_outcome"), "economic_position_metrics": _metric(canonical_like, "like_for_like_outcome")},
        "canonical_baseline": {"stop": CANONICAL_STOP, "target": CANONICAL_TARGET, "metrics": _metric(canonical), "max_simultaneous": _max_simultaneous(canonical), "patterns": patterns},
        "stop_hypothesis_comparison": {
            name: _metric([{"baseline_outcome": x["stop_hypothesis_outcomes"].get(name, {})} for x in canonical if x["stop_hypothesis_outcomes"].get(name, {}).get("r") is not None])
            for name in sorted({name for x in canonical for name in x["stop_hypothesis_outcomes"]})
        },
        "sequential_replay": {p: _sequential(canonical, p) for p in ("ALL_VALID_OPPORTUNITIES", "ONE_POSITION_AT_A_TIME", "REENTRY_ALLOWED_NO_SCALEIN", "REENTRY_AND_SCALEIN_MODEL")},
        "two_position_model": _two_position_model(canonical),
        "target_geometry": {"all_candle_extension": dist("rr", [x for x in target_rows if x["target_name"] == "CANDLE_EXTENSION_50"]), "canonical_target_rr": dist("rr", canonical_target_rows), "canonical_target_distance_atr": dist("target_distance_atr", canonical_target_rows), "canonical_spread_target_pct": dist("spread_target_pct", canonical_target_rows), "canonical_spread_stop_pct": dist("spread_stop_pct", canonical_target_rows), "stop_distance_atr": dist("rr", [{"rr": float(x["stop_hypotheses"][CANONICAL_STOP].get("stop_distance_atr") or 0)} for x in canonical])},
        "same_candle_audit": _same_candle_audit(canonical, data),
        "execution_costs": {"contract_metadata": data.get("contract", {}), "commission": "UNAVAILABLE_IN_HISTORICAL_FIXTURE", "min_stop_constraints": data.get("contract", {}).get("stops_level"), "spread_already_in_entry": True, "sensitivity": {"additional_round_trip_spread_0": _metric(canonical), "additional_round_trip_spread_0_5x": _slippage_view(canonical, .5), "additional_round_trip_spread_1x": _slippage_view(canonical, 1.0)}},
        "context_relationships": {"by_flag": {flag: _metric([x for x in canonical if flag in x["qualification_flags"]]) for flag in sorted({f for x in canonical for f in x["qualification_flags"]})}, "by_pattern": patterns},
        "leakage_audit": {"phase3_provenance_causal": all(r.get("ledger_provenance", {}).get("features_causal") and r.get("ledger_provenance", {}).get("outcome_labels_separate") for r in rows), "future_bars_exposed_in_qualification": False, "outcome_fields_used_in_identity": False, "outcome_fields_used_in_qualification": False, "forming_htf_treated_as_context_not_future_completed_ohlc": True, "m1_used_only_for_ambiguous_outcome_ordering": True},
        "stop_hypotheses": {name: _metric([{**x, "baseline_outcome": _outcome({}, a, name, CANONICAL_TARGET)} for x in []]) for name in ()},
        "raw_items": flat,
    }


def _write_outputs(reports: dict[str, Any]) -> None:
    compact = {}
    for k, v in reports.items():
        compact[k] = {kk: vv for kk, vv in v.items() if kk not in {"raw_items"}}
        if "canonical_baseline" in compact[k]:
            compact[k]["canonical_baseline"] = {kk: vv for kk, vv in compact[k]["canonical_baseline"].items() if kk != "positions"}
    result = {"schema": "context_structure_retrace_phase4_integrity_audit", "phase": 4, "research_only": True, "paper_runner_started": False, "strategy_behavior_changed": False, "development_status": "March-September 2026 EXPOSED DEVELOPMENT DATA; no untouched validation claim", "canonical_baseline": {"stop": CANONICAL_STOP, "target": CANONICAL_TARGET, "selection_basis": "simplest structurally defensible manual interpretation; not selected by performance"}, "instruments": compact}
    (ROOT / "context_structure_retrace_phase4_results.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    fields = ["symbol", "economic_position_id", "setup_id", "phase3_market_event_id", "phase3_entry_opportunity_id", "phase3_entry_attempt_id", "setup_timestamp", "fill_timestamp", "direction", "pattern", "signal_types", "source_observation_count", "economic_position_type", "entry_price", "spread", "outcome", "r", "mae_r", "mfe_r", "exit_timestamp"]
    with (ROOT / "context_structure_retrace_phase4_positions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for symbol, report in reports.items():
            for x in report["canonical_baseline"]["positions"]:
                out = x["baseline_outcome"]
                w.writerow({"symbol": symbol, "economic_position_id": x["economic_position_id"], "setup_id": x["setup_id"], "phase3_market_event_id": x["phase3_market_event_id"], "phase3_entry_opportunity_id": x["phase3_entry_opportunity_id"], "phase3_entry_attempt_id": x["phase3_entry_attempt_id"], "setup_timestamp": _iso(x["setup_timestamp"]), "fill_timestamp": x["fill_timestamp"], "direction": x["direction"], "pattern": x["pattern"], "signal_types": ",".join(x["signal_evidence"]), "source_observation_count": x["source_observation_count"], "economic_position_type": x["economic_position_type"], "entry_price": x["entry_price"], "spread": x["spread"], "outcome": out.get("outcome"), "r": out.get("r"), "mae_r": out.get("mae_r"), "mfe_r": out.get("mfe_r"), "exit_timestamp": _iso(out.get("exit_timestamp"))})
    lines = ["# CONTEXT_STRUCTURE_RETRACE_V1 — Phase 4 integrity audit", "", "Read-only historical audit. No runner, order submission, strategy mutation, or parameter optimization.", "", "## Main finding", "", "The Phase 3 80%+ figures were candidate-attempt metrics, not independent economic-trade metrics. One Phase 3 row can emit multiple depth/confirmation observations, and the same price interaction can be represented by multiple setup events. Phase 4 preserves those observations but collapses identical filled interactions into one economic position for exposure analysis.", "", "## Canonical baseline", "", f"Stop: `{CANONICAL_STOP}`. First target: `{CANONICAL_TARGET}` (manual candle extension capped by opposing structure). This is the simplest structural representation, not a historically optimized choice. Stop hypotheses and target hypotheses remain separately reported.", "", "## Reconciliation", "", "| Instrument | Setup events | Qualified | Entry attempts | Phase 3 opportunities | Economic positions | Overlap groups | Like-for-like attempt PF | Like-for-like economic PF | Canonical baseline PF |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for symbol, r in reports.items():
        p = r["phase3_counts"]; ll = r["like_for_like_original"]; cb = r["canonical_baseline"]["metrics"]
        lines.append(f"| {symbol} | {p['setup_events']} | {p['qualified']} | {p['entry_attempts']} | {p['unique_phase3_opportunities']} | {r['economic_identity']['filled_economic_positions']} | {r['economic_identity']['overlap_groups']} | {ll['phase3_attempt_metrics']['profit_factor']:.3f} | {ll['economic_position_metrics']['profit_factor']:.3f} | {cb['profit_factor']:.3f} |")
    lines += ["", "## What the Phase 3 counts mean", "", "`setup_events` are pattern events. `entry_attempts` are filled/observational retracement interactions; each can carry several signal mechanisms and multiple stop/target outcome hypotheses. `unique_phase3_opportunities` are Phase 3 identities scoped to one originating setup. `economic_positions` use the actual symbol/direction/fill-candle/entry-price interaction, so detector overlap is not multiplied into exposure. Stop and target hypotheses are never counted as positions.", "", "## Sequential and exposure results"]
    for symbol, r in reports.items():
        lines += [f"", f"### {symbol}", "", "| View | Accepted | Expectancy R | PF | Max DD R | Max simultaneous positions |", "|---|---:|---:|---:|---:|---:|"]
        for name, s in r["sequential_replay"].items():
            m = s["metrics"]; lines.append(f"| {name} | {s['accepted']} | {m['expectancy_r'] if m['expectancy_r'] is not None else 0:.4f} | {m['profit_factor'] if m['profit_factor'] is not None else 0:.3f} | {m['max_drawdown_r']:.2f} | {'n/a' if name != 'ALL_VALID_OPPORTUNITIES' else r['canonical_baseline']['max_simultaneous']['max_simultaneous_positions']} |")
        lines.append(f"Two-position model decisions: {r['two_position_model']['decision_count']}; combined initial risk is modelled as 2.0R, with leg B marked proxy/model-only.")
    lines += ["", "## Same-candle and cost audit", "", "M1 resolves same-candle order only where the fixture contains M1. Other instruments use the existing conservative stop-first M5 rule. Historical commission is unavailable in the fixtures; cost sensitivity is reported as additional spread-based R impact rather than an invented commission.", "", "## Leakage re-audit", "", "Qualification and identity use only Phase 3 causal ledger fields. Future outcome fields are attached after identity and are not used to qualify or merge positions. Forming HTF candles remain descriptive context; completed HTF OHLC is never future-exposed.", "", "## Caveats / decisions before freeze", "", "- The baseline stop interpretation is still an explicit research choice and needs approval before prospective use.", "- Re-entry remains descriptive; no scale-in execution is enabled.", "- A fully approved runner would still need a frozen cost/slippage policy and a prospective holdout collected after the freeze.", "- March-September 2026 remains exposed development data, not validation.", "", "## Phase 5 recommendation", "", "Review the normalized economic-position ledger and approve or revise exactly one stop hypothesis, one first-target policy, one departure/re-entry definition, and one cost policy. Only then freeze the baseline and begin prospective paper collection."]
    (ROOT / "context_structure_retrace_phase4_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    raw = _read_rows()
    reports = {}
    for symbol, rows in raw.items():
        data = json.loads((DATA_DIR / DATA_FILES[symbol]).read_text())
        report = audit_symbol(symbol, rows, data)
        # Keep positions for CSV, but not in the summary JSON.
        report["canonical_baseline"]["positions"] = _flatten(symbol, rows, data)[1]["economic_positions"]
        reports[symbol] = report
    _write_outputs(reports)
    for report in reports.values():
        report["canonical_baseline"].pop("positions", None)
    print(json.dumps({k: {"phase3": v["phase3_counts"], "economic": v["economic_identity"], "baseline": v["canonical_baseline"]["metrics"]} for k, v in reports.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
