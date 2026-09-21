"""Read-only replay report for event/opportunity normalization."""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from liquidity_displacement import LiquidityDisplacementConfig, LiquidityDisplacementStrategy
from .normalization import normalize_candidates
from paper_engine import _candle

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path("/tmp/context-structure-retrace")
DATA_FILES = {"XAUUSDm": "xau.json", "BTCUSDm": "btc_basic.json", "USDJPYm": "USDJPYm.json", "EURUSDm": "EURUSDm.json"}
START = int(datetime(2026, 3, 15, tzinfo=timezone.utc).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _bars_before(series: list[dict[str, Any]], ts: int, count: int) -> list[dict[str, Any]]:
    return [x for x in series if int(x["time"]) <= ts][-count:]


def build_candidate(strategy, m5, m15, contract, i, fraction=0.25):
    bar = m5[i]
    ts = int(bar["time"])
    # Data fixtures are aligned M5/M15 series.  Use a bounded index slice so
    # the replay remains linear rather than rescanning the full M15 history.
    m15_i = min(len(m15), i // 3 + 1)
    prior_m15 = m15[max(0, m15_i - 120):m15_i]
    point = float(contract.get("point", contract.get("tick_size", 0.01)))
    spread = float(bar.get("spread", 0)) * point
    mid = float(bar["close"])
    quote = {"bid": mid - spread / 2, "ask": mid + spread / 2}
    candidate = strategy.find_candidate(prior_m15, m5, i, quote, contract, iso(ts + 300))
    if not candidate:
        return None
    d_i = candidate["displacement_index"]
    d = m5[d_i]
    low, high = float(d["low"]), float(d["high"])
    entry = high - (high - low) * fraction if candidate["direction"] == "LONG" else low + (high - low) * fraction
    entry_realistic = entry + spread / 2 if candidate["direction"] == "LONG" else entry - spread / 2
    stop = float(candidate["stop_loss"])
    risk = entry - stop if candidate["direction"] == "LONG" else stop - entry
    if risk <= 0:
        return None
    target = entry + risk * 1.25 if candidate["direction"] == "LONG" else entry - risk * 1.25
    fill_i = None
    for j in range(d_i + 1, min(len(m5), d_i + 1 + strategy.config.max_retrace_candles)):
        c = m5[j]
        touched = float(c["low"]) <= entry if candidate["direction"] == "LONG" else float(c["high"]) >= entry
        held = float(c["close"]) >= entry if candidate["direction"] == "LONG" else float(c["close"]) <= entry
        if touched and held:
            fill_i = j
            break
    row = dict(candidate)
    row.update({
        "symbol": strategy.config.symbol,
        "bar_index": i,
        "sweep_timestamp": int(bar["time"]),
        "displacement_timestamp": int(m5[d_i]["time"]),
        "entry_theoretical": entry,
        "entry_realistic": entry_realistic,
        "stop_loss": stop,
        "risk": risk,
        "target": target,
        "fill_index": fill_i,
        "fill_timestamp": int(m5[fill_i]["time"]) if fill_i is not None else None,
        "spread_price": spread,
        "entry_fraction": fraction,
    })
    if fill_i is not None:
        row.update(outcome(m5, row, fill_i))
    else:
        row.update({"outcome": "UNFILLED", "r": None, "mae": None, "mfe": None, "mae_r": None, "mfe_r": None, "exit_index": None, "exit_timestamp": None})
    return row


def outcome(m5, row, fill_i):
    direction, entry, stop, target = row["direction"], float(row["entry_theoretical"]), float(row["stop_loss"]), float(row["target"])
    risk = abs(entry - stop)
    max_hold = 24
    exit_i, result = None, "TIME_EXIT"
    for j in range(fill_i, min(len(m5), fill_i + max_hold)):
        bar = m5[j]
        stop_hit = float(bar["low"]) <= stop if direction == "LONG" else float(bar["high"]) >= stop
        target_hit = float(bar["high"]) >= target if direction == "LONG" else float(bar["low"]) <= target
        if stop_hit:
            exit_i, result = j, "LOSS"; break
        if target_hit:
            exit_i, result = j, "WIN"; break
    if exit_i is None:
        exit_i = min(len(m5) - 1, fill_i + max_hold - 1)
    exit_bar = m5[exit_i]
    if result == "WIN": r = 1.25
    elif result == "LOSS": r = -1.0
    else:
        close = float(exit_bar["close"])
        r = (close - entry) / risk if direction == "LONG" else (entry - close) / risk
    observed = m5[fill_i:exit_i + 1]
    favorable = max((float(x["high"]) - entry) if direction == "LONG" else (entry - float(x["low"])) for x in observed)
    adverse = max((entry - float(x["low"])) if direction == "LONG" else (float(x["high"]) - entry) for x in observed)
    return {"outcome": result, "r": r, "mae": max(0.0, adverse), "mfe": max(0.0, favorable), "mae_r": max(0.0, adverse) / risk, "mfe_r": max(0.0, favorable) / risk, "exit_index": exit_i, "exit_timestamp": int(exit_bar["time"]), "time_to_target_minutes": (int(exit_bar["time"]) - int(m5[fill_i]["time"])) / 60 if result == "WIN" else None}


def metric(rows):
    resolved = [r for r in rows if r.get("r") is not None and r.get("mae_r") is not None and r.get("mfe_r") is not None]
    rs = [float(r["r"]) for r in resolved]
    wins = [r for r in rs if r > 0]; losses = [r for r in rs if r < 0]
    return {"trades": len(rs), "wins": len(wins), "losses": len(losses), "win_rate_pct": 100 * len(wins) / len(rs) if rs else None, "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": sum(rs) / len(rs) if rs else None, "cumulative_r": sum(rs), "avg_mae_r": statistics.mean(float(r["mae_r"]) for r in resolved) if resolved else None, "avg_mfe_r": statistics.mean(float(r["mfe_r"]) for r in resolved) if resolved else None}


def hypothesis_rows(normalized, m5, name):
    """Re-outcome one representative fill per opportunity under a stop hypothesis."""
    out = []
    for opportunity in normalized["entry_opportunities"]:
        h = opportunity["stop_hypotheses"][name]
        source = next((r for r in opportunity["raw_observations"] if r.get("setup_id") == h["source_setup_id"]), None)
        if not source or source.get("fill_index") is None:
            continue
        row = dict(source)
        row["stop_loss"] = h["stop_loss"]
        row["target"] = h["target"]
        row["risk"] = h["stop_distance"]
        out.append(outcome(m5, row, int(source["fill_index"])))
    return out


def max_exposure(items):
    points = sorted({int(x["fill_index"]) for x in items if x.get("fill_index") is not None})
    maximum = 0.0
    for point in points:
        active = 0.0
        for item in items:
            if item.get("fill_index") is None or int(item["fill_index"]) > point:
                continue
            if item.get("exit_index") is None or point <= int(item["exit_index"]):
                active += float(item.get("risk", 0.0))
        maximum = max(maximum, active)
    return maximum


def replay(symbol: str, fraction=0.25):
    data = json.loads((DATA_DIR / DATA_FILES[symbol]).read_text())
    config = LiquidityDisplacementConfig(symbol=symbol, target_r=1.25, max_hold_minutes=120, max_retrace_candles=5, max_structure_break_candles=5, min_body_atr=.5, min_body_median_multiple=1.0, min_close_location=.6, atr_buffer_fraction=.1)
    strategy = LiquidityDisplacementStrategy(config)
    m5, m15, contract = data["M5"], data["M15"], data["contract"]
    rows = []
    for i, bar in enumerate(m5):
        if int(bar["time"]) >= START:
            row = build_candidate(strategy, m5, m15, contract, i, fraction)
            if row: rows.append(row)
    # Approximate ATR lookup uses the strategy's causal candidate ATR; no future data.
    atr_by_index = {int(r["fill_index"]): float(r.get("atr", 0)) for r in rows if r.get("fill_index") is not None}
    normalized = normalize_candidates(rows, bars=m5, atr_by_index=atr_by_index, departure_atr=2.0)
    return data, rows, normalized


def main():
    reports = {}
    all_rows = []
    for symbol in DATA_FILES:
        try:
            data, rows, normalized = replay(symbol)
        except (FileNotFoundError, KeyError):
            continue
        overlaps = [o for o in normalized["entry_opportunities"] if len(o["candidate_observation_ids"]) > 1]
        filled_opps = [o for o in normalized["entry_opportunities"] if any(r.get("fill_index") is not None for r in o["raw_observations"])]
        candidate_groups = {}
        for r in rows:
            candidate_groups.setdefault((r["direction"], round(float(r["sweep_level"]), 8), r["displacement_index"], round(float(r["entry_theoretical"]), 8), r.get("fill_index")), []).append(r)
        overlapping_groups = [g for g in candidate_groups.values() if len(g) > 1]
        hypothesis_exposure = {}
        for name in ("PRIMARY_DEEPEST", "COMPLETE_SEQUENCE_EXTREME", "MOST_RECENT_NESTED", "DIRECTLY_RESPONSIBLE_INTERNAL"):
            items = []
            for o in filled_opps:
                h = o["stop_hypotheses"][name]
                source = next(r for r in o["raw_observations"] if r.get("setup_id") == h["source_setup_id"])
                items.append({"fill_index": source.get("fill_index"), "exit_index": source.get("exit_index"), "risk": h["stop_distance"]})
            hypothesis_exposure[name] = {"max_simultaneous_risk_price": max_exposure(items), "max_event_correlated_risk_price": max((sum(float(o["stop_hypotheses"][name]["stop_distance"]) for o in filled_opps if o["market_event_id"] == event_id) for event_id in {o["market_event_id"] for o in filled_opps}), default=0.0)}
        exact = next((o for o in normalized["entry_opportunities"] if sorted(x.get("sweep_timestamp") for x in o["raw_observations"]) == [1789511400, 1789511700, 1789512000]), None) if symbol == "BTCUSDm" else None
        exact_exposure = None
        if exact is not None:
            exact_exposure = {
                "raw_candidate_risk_sum": sum(float(r.get("risk", 0.0)) for r in exact["raw_observations"]),
                "normalized_one_opportunity_risk_by_hypothesis": {name: float(exact["stop_hypotheses"][name]["stop_distance"]) for name in exact["stop_hypotheses"]},
                "raw_fill_count": len(exact["raw_observations"]),
                "normalized_entry_attempt_count": 1,
            }
        reports[symbol] = {
            "candidate_observations": len(rows), "filled_candidate_observations": sum(r.get("fill_index") is not None for r in rows),
            "market_events": len(normalized["market_events"]), "sweep_sequences": len(normalized["sweep_sequences"]),
            "entry_opportunities": len(normalized["entry_opportunities"]), "filled_entry_opportunities": len(filled_opps), "re_entries": sum(o["is_reentry"] for o in normalized["entry_opportunities"]),
            "potential_scale_ins": sum(o["potential_scale_in"] for o in normalized["entry_opportunities"]),
            "overlap_opportunities": len(overlaps), "current_overlapping_candidate_groups": len(overlapping_groups), "candidate_metrics": metric(rows),
            "normalized_hypotheses": {name: metric(hypothesis_rows(normalized, data["M5"], name)) for name in ("PRIMARY_DEEPEST", "COMPLETE_SEQUENCE_EXTREME", "MOST_RECENT_NESTED", "DIRECTLY_RESPONSIBLE_INTERNAL")},
            "exposure": {"raw_max_simultaneous_risk_price": max_exposure(rows), "hypotheses": hypothesis_exposure},
            "exact_btc_example": exact,
            "exact_btc_exposure": exact_exposure,
        }
        for r in rows: all_rows.append({"symbol": symbol, **r})
    out = {"scope": {"start": iso(START), "paper_only": True, "strategy_behavior_changed": False, "stop_policy_selected": False}, "departure_definition": {"departure_atr": 2.0, "requires_intervening_bar": True}, "instruments": reports}
    (ROOT / "liquidity_displacement_normalization_results.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    fields = ["symbol", "setup_id", "market_event_id", "sweep_sequence_id", "entry_opportunity_id", "entry_attempt_id", "direction", "sweep_timestamp", "sweep_level", "sweep_extreme", "displacement_timestamp", "fill_timestamp", "entry_theoretical", "stop_loss", "target", "risk", "outcome", "r", "mae_r", "mfe_r"]
    with (ROOT / "liquidity_displacement_normalization_observations.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows({k: r.get(k) for k in fields} for r in all_rows)
    lines = ["# Liquidity-displacement event / opportunity normalization", "", "Read-only historical replay. No forward runner, state, manifest, execution behavior, or stop policy changed.", "", "## Model", "", "Raw sweep candidates are preserved as observations. Candidates sharing symbol, direction, liquidity level, displacement/MSS candle, and theoretical entry are represented as one market event / sweep sequence. A shared fill interaction is one entry opportunity. Stop interpretations remain competing hypotheses.", "", "## Results", "", "| Symbol | Candidates | Filled candidates | Events | Opportunities | Filled opportunities | Re-entries | Overlap groups |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for symbol, r in reports.items(): lines.append(f"| {symbol} | {r['candidate_observations']} | {r['filled_candidate_observations']} | {r['market_events']} | {r['entry_opportunities']} | {r['filled_entry_opportunities']} | {r['re_entries']} | {r['current_overlapping_candidate_groups']} |")
    lines += ["", "## BTC regression", "", "The audited 22:35/22:40/22:45 sequence is represented as one opportunity:", "", "```json", json.dumps(reports.get("BTCUSDm", {}).get("exact_btc_exposure"), indent=2, default=str), "```", "", "The three raw observations remain in the JSON/CSV under one market_event_id and sweep_sequence_id. The entry-attempt count is one. Stop hypotheses remain alternatives; no winner is selected.", "", "## Stop-hypothesis comparison", ""]
    for symbol, r in reports.items():
        lines += [f"### {symbol}", "", "| Hypothesis | Trades | PF | Expectancy R | Cumulative R |", "|---|---:|---:|---:|---:|"]
        for name, m in r["normalized_hypotheses"].items(): lines.append(f"| {name} | {m['trades']} | {m['profit_factor']:.3f} | {m['expectancy_r']:.4f} | {m['cumulative_r']:.2f} |")
    lines += ["", "## Compatibility", "", "The normalization layer is not imported by or enabled in any running forward strategy. Before enabling it, a frozen departure/re-entry policy and an explicitly approved stop policy are still required."]
    (ROOT / "liquidity_displacement_normalization_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"outputs": ["liquidity_displacement_normalization_results.json", "liquidity_displacement_normalization_observations.csv", "liquidity_displacement_normalization_summary.md"], "instruments": reports}, indent=2, default=str))


if __name__ == "__main__":
    main()
