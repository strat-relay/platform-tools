"""Read-only audit of nested BTC25 sweep candidates.

This imports the frozen strategy implementation and only evaluates historical
bars. It does not modify runner state, manifests, or execution behavior.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from liquidity_displacement import LiquidityDisplacementConfig, LiquidityDisplacementStrategy

ROOT = Path(__file__).resolve().parent
DATA = Path("/tmp/context-structure-retrace/btc_basic.json")
START = int(datetime(2026, 3, 15, tzinfo=timezone.utc).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def build_candidate(strategy, m5, m15, contract, i):
    bar = m5[i]
    ts = int(bar["time"])
    prior_m15 = [x for x in m15 if int(x["time"]) <= ts][-120:]
    spread = float(bar.get("spread", 0)) * float(contract.get("point", contract.get("tick_size", 0.01)))
    mid = float(bar["close"])
    quote = {"bid": mid - spread / 2, "ask": mid + spread / 2}
    candidate = strategy.find_candidate(prior_m15, m5, i, quote, contract, iso(ts + 300))
    if not candidate:
        return None
    d = m5[candidate["displacement_index"]]
    low, high = float(d["low"]), float(d["high"])
    entry_theoretical = high - (high - low) * 0.25 if candidate["direction"] == "LONG" else low + (high - low) * 0.25
    entry_realistic = entry_theoretical + spread / 2 if candidate["direction"] == "LONG" else entry_theoretical - spread / 2
    stop = float(candidate["stop_loss"])
    risk = entry_theoretical - stop if candidate["direction"] == "LONG" else stop - entry_theoretical
    if risk <= 0:
        return None
    target = entry_theoretical + risk * 1.25 if candidate["direction"] == "LONG" else entry_theoretical - risk * 1.25
    fill_i = None
    for j in range(candidate["displacement_index"] + 1, min(len(m5), candidate["displacement_index"] + 1 + strategy.config.max_retrace_candles)):
        c = m5[j]
        touched = float(c["low"]) <= entry_theoretical if candidate["direction"] == "LONG" else float(c["high"]) >= entry_theoretical
        held = float(c["close"]) >= entry_theoretical if candidate["direction"] == "LONG" else float(c["close"]) <= entry_theoretical
        if touched and held:
            fill_i = j
            break
    candidate = dict(candidate)
    candidate.update({
        "sweep_timestamp": int(bar["time"]),
        "displacement_timestamp": int(m5[candidate["displacement_index"]]["time"]),
        "entry_theoretical": entry_theoretical,
        "entry_realistic": entry_realistic,
        "stop_loss": stop,
        "risk": risk,
        "target": target,
        "fill_index": fill_i,
        "fill_timestamp": int(m5[fill_i]["time"]) if fill_i is not None else None,
        "spread_price": spread,
    })
    if fill_i is not None:
        candidate.update(outcome(m5, candidate, fill_i))
    else:
        candidate.update({"outcome": "UNFILLED", "r": None, "mae": None, "mfe": None, "time_to_target_minutes": None})
    return candidate


def outcome(m5, candidate, fill_i):
    direction = candidate["direction"]
    entry = float(candidate["entry_theoretical"])
    stop = float(candidate["stop_loss"])
    target = float(candidate["target"])
    risk = abs(entry - stop)
    max_hold_bars = 24
    path = m5[fill_i: min(len(m5), fill_i + max_hold_bars)]
    exit_i = None
    result = "TIME_EXIT"
    for j, bar in enumerate(path):
        hit_stop = float(bar["low"]) <= stop if direction == "LONG" else float(bar["high"]) >= stop
        hit_target = float(bar["high"]) >= target if direction == "LONG" else float(bar["low"]) <= target
        if hit_stop:
            exit_i = fill_i + j; result = "LOSS"; break
        if hit_target:
            exit_i = fill_i + j; result = "WIN"; break
    if exit_i is None:
        exit_i = min(len(m5) - 1, fill_i + max_hold_bars - 1)
    exit_bar = m5[exit_i]
    if result == "WIN":
        r = 1.25
    elif result == "LOSS":
        r = -1.0
    else:
        close = float(exit_bar["close"])
        r = ((close - entry) / risk) if direction == "LONG" else ((entry - close) / risk)
    observed = m5[fill_i:exit_i + 1]
    favorable = max(((float(x["high"]) - entry) if direction == "LONG" else (entry - float(x["low"]))) for x in observed)
    adverse = max(((entry - float(x["low"])) if direction == "LONG" else (float(x["high"]) - entry)) for x in observed)
    return {"outcome": result, "r": r, "mae": max(0.0, adverse), "mfe": max(0.0, favorable), "mae_r": max(0.0, adverse) / risk, "mfe_r": max(0.0, favorable) / risk, "exit_timestamp": int(exit_bar["time"]), "time_to_target_minutes": (int(exit_bar["time"]) - int(m5[fill_i]["time"])) / 60 if result == "WIN" else None}


def metrics(rows):
    resolved = [x for x in rows if x.get("r") is not None]
    rs = [float(x["r"]) for x in resolved]
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]
    return {"n": len(resolved), "wins": len(wins), "losses": len(losses), "win_rate_pct": 100 * len(wins) / len(rs) if rs else None, "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": sum(rs) / len(rs) if rs else None, "cumulative_r": sum(rs), "avg_mae_r": statistics.mean(x["mae_r"] for x in resolved) if resolved else None, "avg_mfe_r": statistics.mean(x["mfe_r"] for x in resolved) if resolved else None}


def selector(group, name):
    if name in {"DEEPEST_EARLIEST", "COMPLETE_SEQUENCE_EXTREME"}:
        return min(group, key=lambda x: (x["sweep_extreme"] if x["direction"] == "LONG" else -x["sweep_extreme"], x["sweep_timestamp"]))
    if name == "MOST_RECENT":
        return max(group, key=lambda x: x["sweep_timestamp"])
    if name == "DIRECTLY_RESPONSIBLE":
        return min(group, key=lambda x: (x["displacement_index"] - x["bar_index"], -x["sweep_timestamp"]))
    raise ValueError(name)


def main():
    data = json.loads(DATA.read_text())
    m5, m15, contract = data["M5"], data["M15"], data["contract"]
    config = LiquidityDisplacementConfig(symbol="BTCUSDm", target_r=1.25, max_hold_minutes=120, max_retrace_candles=5, max_structure_break_candles=5, min_body_atr=.5, min_body_median_multiple=1.0, min_close_location=.6, atr_buffer_fraction=.1)
    strategy = LiquidityDisplacementStrategy(config)
    rows = []
    for i, bar in enumerate(m5):
        if int(bar["time"]) < START:
            continue
        row = build_candidate(strategy, m5, m15, contract, i)
        if row:
            row["bar_index"] = i
            rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        if row["fill_index"] is None:
            continue
        key = (row["direction"], round(row["sweep_level"], 8), row["displacement_index"], round(row["entry_theoretical"], 8), row["fill_index"])
        groups[key].append(row)
    overlaps = [g for g in groups.values() if len(g) > 1]
    group_sizes = Counter(len(g) for g in overlaps)
    selectors = ["DEEPEST_EARLIEST", "MOST_RECENT", "COMPLETE_SEQUENCE_EXTREME", "DIRECTLY_RESPONSIBLE"]
    selector_rows = {}
    for name in selectors:
        selected = [selector(g, name) for g in groups.values() if g]
        selector_rows[name] = metrics(selected)
    all_metrics = metrics(rows)
    result = {"scope": {"symbol": "BTCUSDm", "start": iso(START), "data_file": str(DATA), "paper_only": True}, "logic": {"group_key": "direction + liquidity level + displacement candle + theoretical entry + fill candle", "same_candle_priority": "stop before target", "target": "1.25R from theoretical entry", "future_labels_not_used_for_candidate_generation": True}, "candidate_count": len(rows), "filled_candidate_count": sum(x["fill_index"] is not None for x in rows), "overlap_groups": len(overlaps), "overlap_group_size_distribution": dict(group_sizes), "overlapping_candidate_count": sum(len(g) for g in overlaps), "selector_metrics": selector_rows, "all_candidate_metrics": all_metrics, "exact_btc25_group": [{k: v for k, v in x.items() if k in {"sweep_timestamp", "sweep_level", "sweep_extreme", "displacement_timestamp", "displacement_index", "break_level", "entry_theoretical", "entry_realistic", "stop_loss", "target", "risk", "fill_timestamp", "outcome", "r", "mae", "mfe", "mae_r", "mfe_r", "time_to_target_minutes", "time_sweep_to_break", "break_distance"}} for x in sorted(next(g for g in overlaps if {x["sweep_timestamp"] for x in g} == {1789511400, 1789511700, 1789512000}), key=lambda x: x["sweep_timestamp"])], "provenance": "This is an audit of existing strategy code and historical data. No forward files were written."}
    Path("liquidity_displacement_btc25_sweep_overlap_audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with Path("liquidity_displacement_btc25_sweep_overlap_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["sweep_timestamp", "sweep_level", "sweep_extreme", "displacement_timestamp", "entry_theoretical", "entry_realistic", "stop_loss", "target", "fill_timestamp", "outcome", "r", "mae_r", "mfe_r"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows({k: x.get(k) for k in fields} for x in rows)
    lines = ["# BTC25 sweep-overlap audit", "", "Read-only research. No runner or execution behavior changed.", "", f"Candidate records: {len(rows)}; filled: {result['filled_candidate_count']}; overlapping groups: {len(overlaps)}; overlapping candidates: {result['overlapping_candidate_count']}.", "", "## Exact overlapping BTC25 event", "", json.dumps(result["exact_btc25_group"], indent=2), "", "## Canonical stop hypotheses", "", "| Interpretation | N | Win % | PF | Expectancy R | Cum R | Avg MAE R | Avg MFE R |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in selector_rows.items(): lines.append(f"| {name} | {m['n']} | {m['win_rate_pct']:.2f} | {m['profit_factor'] if m['profit_factor'] is not None else 'N/A'} | {m['expectancy_r']:.4f} | {m['cumulative_r']:.2f} | {m['avg_mae_r']:.3f} | {m['avg_mfe_r']:.3f} |")
    lines += ["", "## Interpretation", "", "The three BTC25 records are nested observations of one liquidity-taking sequence: each sweep candle independently satisfies `low < same liquidity level` and `close > level`, but the implementation has no state saying the level was already reclaimed. The later candles therefore re-arm the same level while price remains in the post-reclaim sequence. Each candidate searches forward to the same displacement/MSS candle and the same retracement fill, while its own sweep extreme remains in the stop formula.", "", "The code does not currently establish a causal rule that one of these extremes is canonical. Deepest/earliest and complete-sequence-extreme are equivalent in this event; most-recent and directly-responsible select the final nested sweep. Until validated as a separate stop hypothesis, these should be treated as alternative risk interpretations of one opportunity, not three independent opportunities."]
    Path("liquidity_displacement_btc25_sweep_overlap_audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"candidate_count": len(rows), "filled": result["filled_candidate_count"], "overlap_groups": len(overlaps), "distribution": dict(group_sizes), "outputs": ["liquidity_displacement_btc25_sweep_overlap_audit.json", "liquidity_displacement_btc25_sweep_overlap_candidates.csv", "liquidity_displacement_btc25_sweep_overlap_audit.md"]}, indent=2))


if __name__ == "__main__":
    main()
