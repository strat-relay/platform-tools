"""Bounded, non-optimizing validation for the structure-sniper family.

This consumes the existing read-only aligned export and writes only research
artifacts.  It intentionally treats the external screenshots as annotations,
never as detector inputs.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .engine import (Bar, H1Scenario, build_structure_map, classify_h1_context,
                     classify_h1_scenario, classify_h4_context, compression_geometry,
                     ema_context, fill_cost, m15_confirmations, pip_size,
                     psychological_levels, sniper_trigger, structural_targets)

ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT / "artifacts/research/multitimeframe_liquidity_sniper/expanded_historical_dataset.json"
OUT = ROOT / "artifacts/research/multitimeframe_structure_sniper"
CONFIG = {
    "name": "STRUCTURE_SNIPER_NEUTRAL_V0", "swing_left": 2, "swing_right": 2,
    "sr_tolerance_atr": 0.25, "compression_tolerance_atr": 0.25,
    "m15_displacement_atr": 0.50, "m15_confirmation": "DISPLACEMENT_OR_LEVEL_CLOSE",
    "m5_family": "MICRO_BOS", "stop_model": "M5_MICRO_SWING",
    "stop_buffer_atr": 0.10, "target_model": "NEAREST_STRUCTURAL_OR_1R",
    "fixed_target_r": 1.0, "max_hold_minutes": 60,
}


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def bars(rows, tf, symbol):
    digits = 3 if "JPY" in symbol.upper() else 5
    point = 0.001 if digits == 3 else 0.00001
    return [Bar(datetime.fromtimestamp(int(x["time"]), timezone.utc), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), tf, spread_points=float(x.get("spread", 0)), digits=digits, point=point) for x in rows]


def raw_trigger(series, i):
    if i < 1:
        return None
    c, p = series[i], series[i - 1]
    if c.close > c.open and c.close > p.high:
        return {"direction": "LONG", "timestamp": c.timestamp, "entry": c.close}
    if c.close < c.open and c.close < p.low:
        return {"direction": "SHORT", "timestamp": c.timestamp, "entry": c.close}
    return None


def metrics(rows):
    fills = [r for r in rows if r.get("fill")]
    gross = [float(r["gross_r"]) for r in fills]
    net = [float(r["net_r"]) for r in fills]
    wins = sum(x > 0 for x in net); losses = sum(x < 0 for x in net)
    return {"candidates": len(rows), "fills": len(fills), "gross_expectancy_R": sum(gross) / len(gross) if gross else None,
            "spread_cost_R": sum(float(r["cost_r"]) for r in fills) / len(fills) if fills else None,
            "net_expectancy_R": sum(net) / len(net) if net else None,
            "PF": sum(x for x in net if x > 0) / abs(sum(x for x in net if x < 0)) if losses else None,
            "win_rate": wins / len(net) if net else None,
            "max_DD_R": max_drawdown(net)}


def max_drawdown(values):
    equity = peak = dd = 0.0
    for v in values:
        equity += v; peak = max(peak, equity); dd = max(dd, peak - equity)
    return dd


def replay_symbol(symbol, data, limit=6000):
    m5 = bars(data["M5"][-limit:], "M5", symbol)
    m15 = bars(data["M15"][-max(1000, limit // 4):], "M15", symbol)
    h1 = bars(data["H1"][-max(400, limit // 12):], "H1", symbol)
    h4 = bars(data["H4"][-max(120, limit // 48):], "H4", symbol)
    groups = {k: [] for k in ("A_M5_ONLY", "B_M15", "C_H1_M15", "D_H4_H1_M15", "E_RANDOM_H1")}
    scenarios = defaultdict(list); ledger = []
    for i, candle in enumerate(m5[1:-1], 1):
        as_of = candle.close_timestamp
        raw = raw_trigger(m5, i)
        if not raw:
            continue
        direction = raw["direction"]
        h1_map = build_structure_map(h1[-300:], as_of, CONFIG["swing_left"], CONFIG["swing_right"])
        h4_map = build_structure_map(h4[-120:], as_of, CONFIG["swing_left"], CONFIG["swing_right"])
        m15_eligible = [b for b in m15 if b.closed_by(as_of)]
        confirms = m15_confirmations(m15_eligible[-80:], as_of, displacement_atr=CONFIG["m15_displacement_atr"])
        confirm = next((x for x in reversed(confirms) if x["direction"] == ("BULLISH" if direction == "LONG" else "BEARISH")), None)
        compression = compression_geometry(h1[-300:], as_of, CONFIG["compression_tolerance_atr"])
        scenario = classify_h1_scenario(h4_map, h1_map, compression)
        m15_ok = confirm is not None
        h1_context = classify_h1_context(h1_map, compression, scenario=scenario,
                                         trade_direction=direction, m15_confirmation=confirm)
        h4_context = classify_h4_context(h4_map, direction)
        h1_ok = h1_context.compatibility.value in {"ALIGNED", "TRANSITION_ACCEPTABLE"}
        h4_ok = h4_context.compatibility.value != "OPPOSING"
        # Deterministic research control: preserve the exact trigger and
        # timestamp, but replace H1 direction with a reproducible pseudo-random
        # label. This is not a trading rule.
        random_label = "BULLISH" if ((i * 1103515245 + 12345) % 2) else "BEARISH"
        conditions = {"A_M5_ONLY": True, "B_M15": m15_ok, "C_H1_M15": m15_ok and h1_ok, "D_H4_H1_M15": m15_ok and h1_ok and h4_ok,
                      "E_RANDOM_H1": m15_ok and random_label == ("BULLISH" if direction == "LONG" else "BEARISH")}
        for group, accepted in conditions.items():
            if not accepted:
                continue
            stop = (min(x.low for x in m5[max(0, i-2):i+1]) - CONFIG["stop_buffer_atr"] * (m5[i].high - m5[i].low)) if direction == "LONG" else (max(x.high for x in m5[max(0, i-2):i+1]) + CONFIG["stop_buffer_atr"] * (m5[i].high - m5[i].low))
            risk = abs(raw["entry"] - stop)
            target = raw["entry"] + risk * CONFIG["fixed_target_r"] if direction == "LONG" else raw["entry"] - risk * CONFIG["fixed_target_r"]
            outcome = None
            for future in m5[i+1:min(len(m5), i+1+CONFIG["max_hold_minutes"] // 5)]:
                hit_stop = future.low <= stop if direction == "LONG" else future.high >= stop
                hit_target = future.high >= target if direction == "LONG" else future.low <= target
                if hit_stop or hit_target:
                    outcome = 1.0 if hit_target and not hit_stop else -1.0
                    break
            spread = (m5[i].ask if hasattr(m5[i], "ask") else None)
            point = m5[i].point or (0.001 if "JPY" in symbol.upper() else 0.00001)
            spread_price = (m5[i].spread_points or 0) * point
            cost = spread_price / risk if risk else None
            row = {"symbol": symbol, "direction": direction, "timestamp": iso(int(candle.timestamp.timestamp())), "control": group,
                   "scenario": scenario.value, "h1_direction": random_label if group == "E_RANDOM_H1" else h1_map.direction, "h4_direction": h4_map.direction,
                   "h1_compatibility": h1_context.compatibility.value, "h4_compatibility": h4_context.compatibility.value,
                   "m15_confirmation": confirm, "entry": raw["entry"], "stop": stop, "target": target,
                   "fill": outcome is not None, "gross_r": outcome, "cost_r": cost, "net_r": outcome - cost if outcome is not None and cost is not None else None,
                   "spread_timestamp": iso(int(candle.timestamp.timestamp())), "spread_pips": spread_price / pip_size(symbol, m5[i].digits or 5, point),
                   "stop_pips": risk / pip_size(symbol, m5[i].digits or 5, point)}
            groups[group].append(row)
            if group == "D_H4_H1_M15":
                scenarios[scenario.value].append(row)
                ledger.append(row)
    return {"metrics": {k: metrics(v) for k, v in groups.items()}, "scenario_metrics": {k: metrics(v) for k, v in scenarios.items()}, "ledger": ledger, "groups": groups}


def external_trace(symbol, data, date_text, observed_entry=None, observed_target=None):
    available = bool(data and any(iso(int(x["time"])).startswith(date_text) for x in data["M5"]))
    if not available:
        return {"symbol": symbol, "date": date_text, "available": False, "EXTERNAL_OUTCOME_USED_FOR_DETECTION": False,
                "failure_stage": "EXTERNAL_PERIOD_UNAVAILABLE", "observed_annotations": {"entry": observed_entry, "target": observed_target}}
    # The trace is causal: only bars whose close precedes each decision are supplied.
    result = replay_symbol(symbol, data, limit=min(6000, len(data["M5"])))
    rows = [r for r in result["ledger"] if r["timestamp"].startswith(date_text)]
    raw_count = sum(1 for i, c in enumerate(data["M5"][:-1]) if iso(int(c["time"])).startswith(date_text) and raw_trigger(bars(data["M5"][max(0, i-1):i+1], "M5", symbol), 1))
    failure = None if rows else "M5_TRIGGER_MISSING_OR_NO_VALID_HTF_SCENARIO"
    return {"symbol": symbol, "date": date_text, "available": True, "EXTERNAL_OUTCOME_USED_FOR_DETECTION": False,
            "causal_summary": {"raw_m5_trigger_count": raw_count, "validated_d_candidate_count": len(rows), "failure_stage": failure},
            "observed_annotations": {"entry": observed_entry, "target": observed_target}, "engine_rows": rows,
            "generated_entry_comparison": [{"generated": r["entry"], "observed": observed_entry, "delta": r["entry"] - observed_entry if observed_entry is not None else None} for r in rows]}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(DATA_PATH.read_text())
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    config_hash = sha(CONFIG); data_hashes = {s: sha(v) for s, v in dataset.items()}
    manifest = {"family": "MULTITIMEFRAME_STRUCTURE_SNIPER_RESEARCH", "mode": "BEHAVIORAL_VALIDATION_ONLY", "config": CONFIG,
                "config_hash": config_hash, "source_commit": source_commit, "data_path": str(DATA_PATH), "data_hashes": data_hashes,
                "optimization_started": False, "promotion": False}
    (OUT / "structure_sniper_neutral_v0.json").write_text(json.dumps(manifest, indent=2) + "\n")
    traces = {
        "USDJPY": external_trace("USDJPYm", dataset["USDJPYm"], "2026-09-10", observed_entry=154.016),
        "GBPUSD": external_trace("GBPUSDm", dataset["GBPUSDm"], "2026-09-10", observed_target=1.334),
    }
    for name, trace in (("external_case_001_usdjpy_trace.json", traces["USDJPY"]), ("external_case_002_gbpusd_trace.json", traces["GBPUSD"])):
        trace.update({"config_hash": config_hash, "source_commit": source_commit, "data_hash": data_hashes.get("USDJPYm" if "usdjpy" in name else "GBPUSDm")})
        (OUT / name).write_text(json.dumps(trace, indent=2, default=str) + "\n")
    replay = {s: replay_symbol(s, dataset[s]) for s in dataset}
    aggregate = {"family": manifest["family"], "mode": manifest["mode"], "config_hash": config_hash, "source_commit": source_commit,
                 "symbols": {}, "scenario_metrics": {}, "context_controls": defaultdict(lambda: defaultdict(list)), "economic_scale": {}}
    ledger_lines = []
    for symbol, result in replay.items():
        aggregate["symbols"][symbol] = result["metrics"]
        for scenario, rows in result["scenario_metrics"].items():
            aggregate["scenario_metrics"].setdefault(scenario, []).append({"symbol": symbol, **rows})
        for group, m in result["metrics"].items():
            aggregate["context_controls"][group]["symbols"].append(symbol)
            aggregate["context_controls"][group]["metrics"].append(m)
        ledger_lines.extend({"config_hash": config_hash, "source_commit": source_commit, **row} for row in result["ledger"])
    aggregate["context_controls"] = {k: dict(v) for k, v in aggregate["context_controls"].items()}
    costs = [float(x["cost_r"]) for x in ledger_lines if x.get("cost_r") is not None]
    aggregate["economic_scale"] = {"fills": len(costs), "median_cost_R": sorted(costs)[len(costs)//2] if costs else None,
                                    "buckets": dict(Counter("<=0.05R" if x <= .05 else "0.05-0.10R" if x <= .10 else "0.10-0.15R" if x <= .15 else "0.15-0.20R" if x <= .20 else "0.20-0.30R" if x <= .30 else ">0.30R" for x in costs))}
    aggregate["historical_data_readiness"] = {"available_symbols": list(dataset), "deep_history_symbols": ["USDJPYm"], "exact_gbpusd_external_period_available": traces["GBPUSD"]["available"], "safe_to_optimize": False}
    (OUT / "neutral_bounded_replay.json").write_text(json.dumps(aggregate, indent=2, default=str) + "\n")
    (OUT / "neutral_bounded_replay_ledger.jsonl").write_text("".join(json.dumps(x, sort_keys=True, default=str) + "\n" for x in ledger_lines))
    comparison = {"USDJPY": traces["USDJPY"], "GBPUSD": traces["GBPUSD"], "human_annotations_are_not_detector_inputs": True,
                  "common_engine_derived_features": "reported only from available traces; no forced agreement"}
    (OUT / "external_case_comparison.json").write_text(json.dumps(comparison, indent=2, default=str) + "\n")
    (OUT / "context_contribution_controls.json").write_text(json.dumps(aggregate["context_controls"], indent=2, default=str) + "\n")
    print(json.dumps({"config_hash": config_hash, "symbols": list(dataset), "usd_available": traces["USDJPY"]["available"], "gbp_available": traces["GBPUSD"]["available"], "ledger_rows": len(ledger_lines), "safe_to_optimize": False}, indent=2))


if __name__ == "__main__":
    main()
