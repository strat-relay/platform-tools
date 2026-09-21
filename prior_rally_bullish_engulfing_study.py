from __future__ import annotations

import csv, json, os, statistics, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import _candle, atr
from engulfing_continuation_study import bullish_engulfing

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/net.metaquotes.wine.metatrader5"
WINPY = r"C:\Python39\python.exe"
SYMBOL = "XAUUSDm"
LOOKBACK_DAYS = 184
DISCOVERY_END = "2026-06"
HORIZONS = (1, 2, 3, 6, 12)
TARGETS = (0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00)


def v(b, k): return _candle(b, k)
def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def month(t): return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")
def pct(n, d): return 100.0 * n / d if d else 0.0


def fetch():
    env = dict(os.environ, WINEPREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5")
    p = subprocess.run(["/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine", WINPY, "Z:" + str(ROOT / "historical_fetch_symbol.py"), SYMBOL], env=env, text=True, capture_output=True, timeout=180, check=True)
    return json.loads(p.stdout.splitlines()[-1])


def rally_flags(bars, i):
    # All references are completed candles strictly before the engulfing bar i.
    pre = bars[i - 1]
    a = v(pre, "close") > v(bars[i - 3], "open")
    b = v(pre, "close") > v(bars[i - 6], "open")
    c = v(pre, "close") > v(bars[i - 4], "close")
    d = v(pre, "close") > v(bars[i - 7], "close")
    last3 = bars[i - 3:i]
    e = all(v(last3[j], "high") > v(last3[j - 1], "high") and v(last3[j], "low") > v(last3[j - 1], "low") for j in (1, 2))
    return {"A_PRIOR_3_POSITIVE_NET": a, "B_PRIOR_6_POSITIVE_NET": b, "C_PRE_CLOSE_ABOVE_3_BACK": c, "D_PRE_CLOSE_ABOVE_6_BACK": d, "E_PRE_HIGHER_HIGH_LOWS": e}


def decline_flags(flags, bars, i):
    pre = bars[i - 1]
    return {"A_PRIOR_3_POSITIVE_NET": v(pre, "close") < v(bars[i - 3], "open"), "B_PRIOR_6_POSITIVE_NET": v(pre, "close") < v(bars[i - 6], "open"), "C_PRE_CLOSE_ABOVE_3_BACK": v(pre, "close") < v(bars[i - 4], "close"), "D_PRE_CLOSE_ABOVE_6_BACK": v(pre, "close") < v(bars[i - 7], "close"), "E_PRIOR_LOWER_HIGH_LOWS": all(v(bars[i - 3 + j], "high") < v(bars[i - 3 + j - 1], "high") and v(bars[i - 3 + j], "low") < v(bars[i - 3 + j - 1], "low") for j in (1, 2))}


def outcome(entry, stop, bars, start, spread_point):
    risk = entry - stop; out = {"stop_distance": risk}
    for h in HORIZONS:
        w = bars[start:start + h]; highs = [v(x, "high") for x in w]; lows = [v(x, "low") for x in w]
        if not w: continue
        exit_bid = v(w[-1], "close") - float(w[-1].get("spread", 0)) * spread_point / 2
        out[f"net_{h}_r"] = (exit_bid - entry) / risk
        out[f"mfe_{h}_r"] = (max(highs) - entry) / risk; out[f"mae_{h}_r"] = (entry - min(lows)) / risk
        out[f"high_taken_{h}"] = max(highs) > bars[start - 1]["high"]
        out[f"low_taken_{h}"] = min(lows) <= bars[start - 1]["low"]
    out["targets"] = {}
    for target in TARGETS:
        tp = entry + risk * target; hit = None; r = None
        for j, b in enumerate(bars[start:start + 12], start=1):
            sl = v(b, "low") <= stop; take = v(b, "high") >= tp
            if sl or take:
                hit = "STOP" if sl else "TARGET"; r = -1.0 if sl else target; out["targets"][str(target)] = {"result": hit, "r": r, "bars": j}; break
        if hit is None:
            b = bars[min(start + 11, len(bars) - 1)]; bid = v(b, "close") - float(b.get("spread", 0)) * spread_point / 2; out["targets"][str(target)] = {"result": "TIME_EXIT", "r": (bid - entry) / risk, "bars": 12}
    return out


def metrics(rows, key="net_6_r"):
    rs = [float(x[key]) for x in rows if key in x]; wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]; eq = peak = dd = 0.0
    for x in rs: eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {"n": len(rows), "wins": len(wins), "losses": len(losses), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": statistics.mean(rs) if rs else 0.0, "median_r": statistics.median(rs) if rs else 0.0, "max_drawdown_r": dd, "cumulative_r": sum(rs)}


def target_metrics(rows):
    out = {}
    for target in TARGETS:
        vals = [x["targets"][str(target)]["r"] for x in rows]; wins = [x for x in vals if x > 0]; losses = [x for x in vals if x < 0]
        out[str(target)] = {"n": len(vals), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": statistics.mean(vals) if vals else 0.0, "cumulative_r": sum(vals), "time_exits": sum(x["targets"][str(target)]["result"] == "TIME_EXIT" for x in rows)}
    return out


def main():
    data = fetch(); bars = data["M5"]; contract = data["contract"]; spread_point = float(contract["point"]); end = max(int(x["time"]) for x in bars[:-1]); start = end - LOOKBACK_DAYS * 86400; work = [x for x in bars if start <= int(x["time"]) < end]; events = []; rally_only = []
    for i in range(40, len(work) - 13):
        flags = rally_flags(work, i); declines = decline_flags(flags, work, i); a = atr(work[i - 40:i + 1])[-1] or 0.0; pre = work[i - 1]; spread = float(work[i].get("spread", 0)) * spread_point
        # Prior-rally-only control: entry at every eligible pre-engulfing close, no engulfing requirement.
        for name, passed in flags.items():
            if passed:
                entry = v(pre, "close") + spread / 2; stop = v(pre, "low") - max(a * .10, spread * 1.25, float(contract["tick_size"])); row = {"condition": name, "timestamp": iso(int(pre["time"])), "month": month(int(pre["time"])), "entry": entry}; row.update(outcome(entry, stop, work, i, spread_point)); rally_only.append(row)
        d = bullish_engulfing(work, i)
        if not d: continue
        entry = v(work[i], "close") + spread / 2; stop = v(work[i], "low") - max(a * .10, spread * 1.25, float(contract["tick_size"])); row = {"setup_id": f"PRBE-{int(work[i]['time'])}", "timestamp": iso(int(work[i]["time"])), "month": month(int(work[i]["time"])), "prior_rally": flags, "prior_decline": declines, "entry": entry, "stop": stop, "atr": a, "spread": spread, "body_atr": d["body_atr"]}; row.update(outcome(entry, stop, work, i + 1, spread_point)); events.append(row)
    discovery = lambda rows: [x for x in rows if x["month"] <= DISCOVERY_END]; validation = lambda rows: [x for x in rows if x["month"] > DISCOVERY_END]; all_d, all_v = discovery(events), validation(events); definitions = list(rally_flags(work, 40).keys()); candidates = {}
    decline_key = {"A_PRIOR_3_POSITIVE_NET": "A_PRIOR_3_POSITIVE_NET", "B_PRIOR_6_POSITIVE_NET": "B_PRIOR_6_POSITIVE_NET", "C_PRE_CLOSE_ABOVE_3_BACK": "C_PRE_CLOSE_ABOVE_3_BACK", "D_PRE_CLOSE_ABOVE_6_BACK": "D_PRE_CLOSE_ABOVE_6_BACK", "E_PRE_HIGHER_HIGH_LOWS": "E_PRIOR_LOWER_HIGH_LOWS"}
    for name in definitions:
        dk = decline_key[name]
        candidates[name] = {"rally_discovery": [x for x in all_d if x["prior_rally"][name]], "rally_validation": [x for x in all_v if x["prior_rally"][name]], "decline_discovery": [x for x in all_d if x["prior_decline"].get(dk, False)], "decline_validation": [x for x in all_v if x["prior_decline"].get(dk, False)]}
    # Selection uses only discovery six-bar results, with a minimum sample of 50.
    selected = max((n for n, x in candidates.items() if len(x["rally_discovery"]) >= 50), key=lambda n: metrics(candidates[n]["rally_discovery"])["expectancy_r"], default=None)
    controls = {"ALL_BULLISH_ENGULFINGS": {"discovery": all_d, "validation": all_v}}
    if selected:
        controls["SELECTED_PRIOR_RALLY_PLUS_ENGULFING"] = {"discovery": candidates[selected]["rally_discovery"], "validation": candidates[selected]["rally_validation"]}
        controls["SELECTED_PRIOR_DECLINE_PLUS_ENGULFING"] = {"discovery": candidates[selected]["decline_discovery"], "validation": candidates[selected]["decline_validation"]}
        controls["SELECTED_PRIOR_RALLY_WITHOUT_ENGULFING"] = {"discovery": discovery([x for x in rally_only if x["condition"] == selected]), "validation": validation([x for x in rally_only if x["condition"] == selected])}
    summary = {"symbol": SYMBOL, "paper_only": True, "period": {"start": iso(start), "end": iso(end), "discovery_end": DISCOVERY_END}, "rules": {"engulfing": "exact body-only bullish engulfing", "entry": "ask immediately after completed engulfing close", "stop": "below engulfing low plus fixed existing minimal ATR/spread buffer", "outcome_horizon": HORIZONS}, "selected_definition_discovery_only": selected, "all_bullish": {"discovery": metrics(all_d), "validation": metrics(all_v), "targets_discovery": target_metrics(all_d), "targets_validation": target_metrics(all_v)}, "definitions": {}, "controls": {}, "high_take_probabilities": {}}
    for name, x in candidates.items(): summary["definitions"][name] = {"rally": {"discovery": metrics(x["rally_discovery"]), "validation": metrics(x["rally_validation"]), "targets_discovery": target_metrics(x["rally_discovery"]), "targets_validation": target_metrics(x["rally_validation"])}, "decline": {"discovery": metrics(x["decline_discovery"]), "validation": metrics(x["decline_validation"]), "targets_discovery": target_metrics(x["decline_discovery"]), "targets_validation": target_metrics(x["decline_validation"])}}
    for name, x in controls.items(): summary["controls"][name] = {"discovery": metrics(x["discovery"]), "validation": metrics(x["validation"])}
    for label, rows in {"ALL": all_d + all_v, "PRIOR_RALLY_SELECTED": controls.get("SELECTED_PRIOR_RALLY_PLUS_ENGULFING", {}).get("discovery", []) + controls.get("SELECTED_PRIOR_RALLY_PLUS_ENGULFING", {}).get("validation", [])}.items(): summary["high_take_probabilities"][label] = {str(h): pct(sum(x[f"high_taken_{h}"] for x in rows), len(rows)) for h in HORIZONS}
    fields = sorted({k for r in events for k in r}); fields += ["discovery_split"]
    with open(ROOT / "prior_rally_bullish_engulfing_samples.csv", "w", newline="", encoding="utf-8") as f: w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); [w.writerow({**r, "discovery_split": "DISCOVERY" if r["month"] <= DISCOVERY_END else "VALIDATION"}) for r in events]
    (ROOT / "prior_rally_bullish_engulfing_results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = ["# Prior rally → bullish engulfing pre-entry study", "", "READ-ONLY research. No strategy modified.", "", f"Selected from discovery only: {selected}", "", "| Condition | Discovery N | Exp R | PF | Validation N | Exp R | PF |", "|---|---:|---:|---:|---:|---:|---:|"]
    for label, x in summary["controls"].items(): lines.append(f"| {label} | {x['discovery']['n']} | {x['discovery']['expectancy_r']:.4f} | {x['discovery']['profit_factor']} | {x['validation']['n']} | {x['validation']['expectancy_r']:.4f} | {x['validation']['profit_factor']} |")
    lines += ["", "## Definition comparison", json.dumps(summary["definitions"], indent=2), "", "## Controls", json.dumps(summary["controls"], indent=2), "", "## High-take probabilities", json.dumps(summary["high_take_probabilities"], indent=2), "", "## Conclusion", "The rally definition was selected using discovery only. Validation is reported once and was not used to change the definition."]
    (ROOT / "prior_rally_bullish_engulfing_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"selected": selected, "controls": summary["controls"], "all_bullish": summary["all_bullish"]}, indent=2))


if __name__ == "__main__": main()
