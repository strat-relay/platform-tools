from __future__ import annotations

import bisect
import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from liquidity_displacement import LiquidityDisplacementStrategy
from liquidity_displacement_validate import fetch, iso, pct, session, trend
from liquidity_displacement_v1_variant_a import VA_CFG

ROOT = Path(__file__).resolve().parent
TARGETS = [1.0, 1.25, 1.5]


def outcome(row, bars, fill_i, target):
    entry, stop = float(row["entry"]), float(row["stop_loss"])
    risk, long = abs(entry - stop), row["direction"] == "LONG"
    tp = entry + risk * target if long else entry - risk * target
    for step in range(25):
        bar = bars[fill_i + step]
        hit_sl = float(bar["low"]) <= stop if long else float(bar["high"]) >= stop
        hit_tp = float(bar["high"]) >= tp if long else float(bar["low"]) <= tp
        if step >= 24:
            px = float(bar["close"])
            return {"outcome": "TIME_EXIT", "r": (px - entry) / risk if long else (entry - px) / risk, "duration_minutes": step * 5}
        if hit_sl or hit_tp:
            px = stop if hit_sl else tp
            return {"outcome": "LOSS" if hit_sl else "WIN", "r": (px - entry) / risk if long else (entry - px) / risk, "duration_minutes": step * 5}
    return {"outcome": "INVALID_AFTER_SIGNAL", "r": None, "duration_minutes": 120}


def metrics(rows):
    filled = [x for x in rows if x["status"] == "FILLED"]
    resolved = [x for x in filled if x.get("r") is not None]
    rs = [float(x["r"]) for x in resolved]
    wins, losses = [x for x in resolved if x["outcome"] == "WIN"], [x for x in resolved if x["outcome"] == "LOSS"]
    eq = peak = dd = 0.0
    streak = longest = 0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
        streak = streak + 1 if r < 0 else 0; longest = max(longest, streak)
    gp, gl = sum(float(x["r"]) for x in wins), sum(float(x["r"]) for x in losses)
    durations = [x["duration_minutes"] for x in resolved]
    return {"setups": len(rows), "filled": len(filled), "fill_rate_pct": pct(len(filled), len(rows)), "unfilled": sum(x["status"] == "UNFILLED" for x in rows), "resolved_trades": len(resolved), "wins": len(wins), "losses": len(losses), "time_exits": sum(x["outcome"] == "TIME_EXIT" for x in resolved), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": gp / abs(gl) if gl else None, "expectancy_r": sum(rs) / len(rs) if rs else 0.0, "cumulative_r": sum(rs), "max_drawdown_r": dd, "longest_losing_streak": longest, "average_duration_minutes": statistics.mean(durations) if durations else 0.0, "median_duration_minutes": statistics.median(durations) if durations else 0.0}


def main():
    data = fetch(); bars = data["M5"]; end = max(int(x["time"]) for x in bars[:-1]); start = end - 184 * 86400
    m5 = [x for x in bars if start <= int(x["time"]) < end]; m15 = data["M15"]; m15_times = [int(x["time"]) for x in m15]; contract = data["contract"]
    strategy = LiquidityDisplacementStrategy(VA_CFG); setups = []
    for i in range(40, len(m5) - 25):
        t = int(m5[i]["time"]); ci = bisect.bisect_right(m15_times, t); context = m15[max(0, ci - 120):ci]
        spread = float(m5[i]["spread"]) * float(contract["point"]); quote = {"bid": float(m5[i]["close"]) - spread / 2, "ask": float(m5[i]["close"]) + spread / 2}
        candidate = strategy.evaluate(context, m5, quote, contract, iso(t + 300), i)
        if not candidate: continue
        fill_i = candidate["fill_index"]; event_i = fill_i if candidate["status"] == "FILLED" else candidate["displacement_index"]; event_time = int(m5[event_i]["time"]) + 300; dt = datetime.fromtimestamp(event_time, timezone.utc); risk = float(candidate["risk"])
        h1 = [x for x in data["H1"] if int(x["time"]) <= t][-120:]
        setups.append({"setup_id": f"LDSVA-{t}-{i}-{candidate['direction']}", "timestamp": iso(event_time), "month": dt.strftime("%Y-%m"), "session": session(event_time), "direction": candidate["direction"], "setup_type": candidate["setup_type"], "entry_type": candidate["entry_type"], "entry": candidate["entry"], "stop_loss": candidate["stop_loss"], "stop_distance": risk, "spread": candidate["spread"], "spread_stop_ratio": candidate["spread"] / risk if risk else 0, "atr": candidate["atr"], "h1_trend": trend(h1), "m15_trend": trend(context), "status": candidate["status"], "fill_index": fill_i, "min_lot_risk_dollars": (risk / contract["tick_size"]) * contract["tick_value"] * contract["min_lot"]})
    by_target = {}
    for target in TARGETS:
        rows = []
        for setup in setups:
            row = dict(setup); row["target_r"] = target; row.update(outcome(setup, m5, int(setup["fill_index"]), target) if setup["status"] == "FILLED" else {"outcome": "UNFILLED", "r": None, "duration_minutes": 0}); rows.append(row)
        by_target[str(target)] = rows
    base = by_target["1.25"]; months = sorted({x["month"] for x in base}); split = max(1, len(months) - 2); discovery, validation = months[:split], months[split:]
    v1 = json.loads((ROOT / "liquidity_displacement_results.json").read_text())
    result = {"strategy": "LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A", "rules": {"max_retrace_candles": 5, "target_grid": TARGETS, "other_rules": "same as V1"}, "period": {"start": iso(start), "end": iso(end)}, "source_sha256": "4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea", "setups_count": len(setups), "target_results": {k: metrics(v) for k, v in by_target.items()}, "monthly_1.25R": {m: metrics([x for x in base if x["month"] == m]) for m in months}, "discovery_validation": {"discovery_months": discovery, "validation_months": validation, "discovery": metrics([x for x in base if x["month"] in discovery]), "validation": metrics([x for x in base if x["month"] in validation])}, "comparison_v1_1_25R": {"v1": v1["target_results"]["1.25"], "variant_a": metrics(base)}}
    (ROOT / "liquidity_displacement_v1_variant_a_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    fields = list(base[0].keys()) if base else ["setup_id"]
    with (ROOT / "liquidity_displacement_v1_variant_a_trades.csv").open("w", newline="", encoding="utf-8") as f: csv.DictWriter(f, fieldnames=fields).writeheader(); csv.DictWriter(f, fieldnames=fields).writerows(base)
    summary = ["# LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A historical validation", "", "READ-ONLY / PAPER-ONLY. V1 was not modified.", "", "## Fixed changes", "Retracement expiry increased from 3 to 5 completed M5 candles. Targets compared at 1.0R, 1.25R, and 1.5R. No other rule changed.", "", "## Target results", json.dumps(result["target_results"], indent=2), "", "## Monthly 1.25R", json.dumps(result["monthly_1.25R"], indent=2), "", "## Discovery / validation", json.dumps(result["discovery_validation"], indent=2), "", "## V1 comparison", json.dumps(result["comparison_v1_1_25R"], indent=2)]
    (ROOT / "liquidity_displacement_v1_variant_a_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps({"setups": len(setups), "targets": result["target_results"], "validation": result["discovery_validation"]["validation"]}, indent=2))


if __name__ == "__main__": main()
