from __future__ import annotations

import csv, json, math, os, random, statistics, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import _candle, atr
from simple_sr_candle_validate import levels_before, support_match, swing_points
from engulfing_continuation_study import bullish_engulfing

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX = "/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY = r"C:\Python39\python.exe"
SYMBOL = "XAUUSDm"
START_DAYS = 184
DISCOVERY_END_MONTH = "2026-06"
RISK_LEVEL = 1.0


def v(b, k): return _candle(b, k)
def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def month(t): return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")
def pct(n, d): return 100.0 * n / d if d else 0.0
def session(t):
    h = datetime.fromtimestamp(int(t), timezone.utc).hour
    return "ASIA" if h < 8 else "LONDON" if h < 13 else "OVERLAP" if h < 17 else "NEW_YORK" if h < 22 else "OTHER"


def fetch_all():
    env = dict(os.environ, WINEPREFIX=PREFIX)
    p = subprocess.run([WINE, WINPY, "Z:" + str(ROOT / "historical_fetch_symbol.py"), SYMBOL], env=env, text=True, capture_output=True, timeout=180, check=True)
    q = subprocess.run([WINE, WINPY, "Z:" + str(ROOT / "historical_fetch_m1.py"), SYMBOL], env=env, text=True, capture_output=True, timeout=180, check=True)
    return json.loads(p.stdout.splitlines()[-1]), json.loads(q.stdout.splitlines()[-1])


def m1_order(m1, start, end, high_level, adverse_level):
    x = [b for b in m1 if start <= int(b["time"]) < end]
    hi = next((int(b["time"]) for b in x if v(b, "high") >= high_level), None)
    lo = next((int(b["time"]) for b in x if v(b, "low") <= adverse_level), None)
    return {"first_high_or_adverse": "HIGH" if hi is not None and (lo is None or hi < lo) else "ADVERSE" if lo is not None else "UNRESOLVED", "m1_high_time": iso(hi) if hi else None, "m1_adverse_time": iso(lo) if lo else None}


def result_metrics(rows):
    rs = [float(x["six_bar_r"]) for x in rows if x.get("six_bar_r") is not None]
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]; eq = peak = dd = 0.0
    for x in rs:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    boot = []
    if len(rs) >= 20:
        rng = random.Random(7)
        for _ in range(500): boot.append(statistics.mean(rng.choices(rs, k=len(rs))))
    return {"sample": len(rows), "wins": len(wins), "losses": len(losses), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": statistics.mean(rs) if rs else 0.0, "expectancy_bootstrap_95ci": [sorted(boot)[12], sorted(boot)[487]] if boot else None, "cumulative_r": sum(rs), "max_drawdown_r": dd}


def build_events(data, m1):
    bars = data["M5"]; m15 = data["M15"]; contract = data["contract"]; end = max(int(x["time"]) for x in bars[:-1]); start = end - START_DAYS * 86400; work = [x for x in bars if start <= int(x["time"]) < end]; m15_times = [int(x["time"]) for x in m15]; cache = {}; events = []; confirmed_points = swing_points(m15, 2)
    for i in range(40, len(work) - 13):
        d = bullish_engulfing(work, i)
        if not d: continue
        t = int(work[i]["time"]); a = atr(work[i - 40:i + 1])[-1] or 0.0; d["body_atr"] = d["body"] / max(a, 1e-12); prior3 = (v(work[i], "close") / v(work[i - 3], "open") - 1.0); prior6 = (v(work[i], "close") / v(work[i - 6], "open") - 1.0); prior12 = (v(work[i], "close") / v(work[i - 12], "open") - 1.0)
        prior6high = max(v(x, "high") for x in work[i - 6:i]); prior6low = min(v(x, "low") for x in work[i - 6:i]); prior12high = max(v(x, "high") for x in work[i - 12:i]); prior12low = min(v(x, "low") for x in work[i - 12:i]); spread = float(work[i].get("spread", 0)) * float(contract["point"]); ask = v(work[i], "close") + spread / 2; buffer = max(a * .10, spread * 1.25, float(contract["tick_size"])); stop = min(v(work[i], "low"), prior6low) - buffer; risk = ask - stop
        if risk <= 0: continue
        mi = __import__("bisect").bisect_left(m15_times, t) - 1; mi = max(mi, 0)
        if mi not in cache: cache[mi] = levels_before(m15[max(0, mi - 480):mi], t, confirmed_points)
        levels = cache[mi]; support = support_match(levels, work[i], t, a); resist = [z for z in levels if z["type"] == "RESISTANCE" and v(work[i], "low") <= z["zone_high"] + a*.25 and v(work[i], "high") >= z["zone_low"] - a*.25]
        after = work[i + 1:i + 7]; high_taken = next((j + 1 for j, b in enumerate(after) if v(b, "high") > v(work[i], "high")), None); high_bar = after[high_taken - 1] if high_taken else None; max_high = max(v(b, "high") for b in after); min_low = min(v(b, "low") for b in after); post_high_low = min([v(b, "low") for b in after[high_taken:]] or [min_low]) if high_taken else min_low; cont = bool(high_taken and max_high - v(work[i], "high") >= .50 * a and v(after[-1], "close") >= v(work[i], "close")); reject = bool(high_taken and (v(after[-1], "close") <= v(work[i], "close") or post_high_low <= v(work[i], "high") - .50 * a) and not cont); outcome = "CONTINUATION" if cont else "HIGH_TAKE_REJECTION" if reject else "NO_HIGH_TAKE" if not high_taken else "RANGE_INDECISION"; close6 = v(after[-1], "close"); six_r = (close6 - ask) / risk
        seq = m1_order(m1, t, int(after[min(1, len(after)-1)]["time"]) + 300, v(work[i], "high"), ask - risk) if m1 else {}
        pre_high_bars = after[:high_taken-1] if high_taken and high_taken > 1 else []
        pullback_before = (v(work[i], "close") - min([v(b, "low") for b in pre_high_bars] or [v(work[i], "close")])) / a
        events.append({"event_id": f"EPD-{t}", "timestamp": iso(t), "month": month(t), "session": session(t), "outcome_class": outcome, "prior3_return": prior3, "prior6_return": prior6, "prior12_return": prior12, "prior6_high_distance_atr": (prior6high - v(work[i], "close"))/a, "prior6_low_distance_atr": (v(work[i], "close") - prior6low)/a, "range_position_12": (v(work[i], "close") - prior12low) / max(prior12high - prior12low, 1e-12), "atr": a, "spread": spread, "spread_atr": spread/max(a,1e-12), "body_atr": d["body_atr"], "body_vs_previous": d["body_vs_previous"], "close_location": d["close_location"], "upper_wick_body": d["upper_wick"]/max(d["body"],1e-12), "lower_wick_body": d["lower_wick"]/max(d["body"],1e-12), "sweeps_prior_low": v(work[i], "low") < prior6low, "breaks_prior_high": v(work[i], "close") > prior6high, "near_support": bool(support), "near_resistance": bool(resist), "m15_context": "BULLISH" if v(m15[mi], "close") > v(m15[max(0,mi-4)], "close") else "BEARISH", "high_taken_within": high_taken, "max_above_high_atr": (max_high - v(work[i], "high"))/a, "pullback_before_high_atr": pullback_before, "six_bar_mfe_r": (max_high - ask)/risk, "six_bar_mae_r": (ask - min_low)/risk, "six_bar_r": six_r, "high_take_m1_order": seq.get("first_high_or_adverse")})
    return events, start, end


def candidates(rows):
    return {"SWEEP_LOW": lambda r: r["sweeps_prior_low"], "PRIOR_RALLY_HIGH_TAKE": lambda r: r["prior6_return"] > 0 and r["high_taken_within"] is not None, "STRONG_CLOSE": lambda r: r["close_location"] >= .75 and r["body_atr"] >= 1.0, "NEAR_RESISTANCE": lambda r: r["near_resistance"], "BREAK_PRIOR_HIGH": lambda r: r["breaks_prior_high"]}


def main():
    data, m1 = fetch_all(); events, start, end = build_events(data, m1); split = [x for x in events if x["month"] <= DISCOVERY_END_MONTH]; val = [x for x in events if x["month"] > DISCOVERY_END_MONTH]; baseline = result_metrics(split); definitions = candidates(events); rows = []
    for name, fn in definitions.items():
        d = [x for x in split if fn(x)]; v = [x for x in val if fn(x)]; rows.append({"sequence": name, "discovery": {"sample": len(d), "frequency_pct": pct(len(d), len(split)), "outcome_counts": Counter(x["outcome_class"] for x in d), "metrics": result_metrics(d), "monthly": {m: result_metrics([x for x in d if x["month"] == m]) for m in sorted(set(x["month"] for x in d))}}, "validation": {"sample": len(v), "frequency_pct": pct(len(v), len(val)), "outcome_counts": Counter(x["outcome_class"] for x in v), "metrics": result_metrics(v), "monthly": {m: result_metrics([x for x in v if x["month"] == m]) for m in sorted(set(x["month"] for x in v))}}})
    ranked = sorted(rows, key=lambda x: (x["discovery"]["sample"] >= 50, abs(x["discovery"]["metrics"]["expectancy_r"] - baseline["expectancy_r"])), reverse=True)
    out = {"symbol": SYMBOL, "paper_only": True, "period": {"start": iso(start), "end": iso(end), "discovery_end_month": DISCOVERY_END_MONTH}, "definitions": {"bullish_engulfing": "previous bearish, current bullish, current body engulfs previous real body", "continuation": "high taken within six M5 bars, at least 0.5 ATR beyond engulfing high, final close at/above engulfing close", "high_take_rejection": "high taken, then final close back at/below engulfing close or post-take low retreats at least 0.5 ATR", "no_high_take": "high not exceeded within six M5 bars", "risk": "ask after engulfing close; stop below min(engulfing low, prior six-bar low) plus buffer; six-bar close R"}, "base_rates": {"all_events": len(events), "outcomes": Counter(x["outcome_class"] for x in events), "discovery_metrics": baseline, "validation_metrics": result_metrics(val)}, "feature_comparison": {}, "top_sequences": ranked[:5], "m1_order_coverage": Counter(x.get("high_take_m1_order") for x in events), "data_quality": ["M1 was used to annotate early high/adverse ordering where available.", "Outcome classes use fixed descriptive thresholds set before ranking; they are not optimized on validation."]}
    for field in ("prior3_return", "prior6_return", "prior12_return", "body_atr", "body_vs_previous", "close_location", "upper_wick_body", "lower_wick_body", "spread_atr", "range_position_12", "max_above_high_atr"):
        a = [x[field] for x in split if x["outcome_class"] == "CONTINUATION"]; b = [x[field] for x in split if x["outcome_class"] == "HIGH_TAKE_REJECTION"]; out["feature_comparison"][field] = {"continuation_n": len(a), "rejection_n": len(b), "continuation_median": statistics.median(a) if a else None, "rejection_median": statistics.median(b) if b else None, "median_difference": (statistics.median(a)-statistics.median(b)) if a and b else None}
    with open(ROOT / "engulfing_pattern_sequences.csv", "w", newline="", encoding="utf-8") as f:
        fields = sorted({k for r in events for k in r}); w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(events)
    (ROOT / "engulfing_pattern_discovery.json").write_text(json.dumps(out, indent=2, default=lambda x: dict(x) if isinstance(x, Counter) else x), encoding="utf-8")
    lines = ["# XAUUSDm engulfing pattern discovery", "", "READ-ONLY research. No strategy or runner modified.", "", f"Events: {len(events)} · discovery: {len(split)} · validation: {len(val)}", "", "## Base rates", json.dumps(out["base_rates"], indent=2, default=lambda x: dict(x) if isinstance(x, Counter) else x), "", "## Top predefined sequence families", "| Sequence | Discovery n | Discovery expectancy | Validation n | Validation expectancy | Validation PF |", "|---|---:|---:|---:|---:|---:|"]
    for x in ranked[:5]: lines.append(f"| {x['sequence']} | {x['discovery']['sample']} | {x['discovery']['metrics']['expectancy_r']:.4f} | {x['validation']['sample']} | {x['validation']['metrics']['expectancy_r']:.4f} | {x['validation']['metrics']['profit_factor']} |")
    lines += ["", "## Feature distribution comparison", json.dumps(out["feature_comparison"], indent=2), "", "## Conclusion", "Top sequences are predefined descriptive families, not a fitted strategy. Any sequence with weak validation, small sample, or unstable sign is not robust."]
    (ROOT / "engulfing_pattern_discovery_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"events": len(events), "discovery": len(split), "validation": len(val), "base_rates": out["base_rates"], "top_sequences": ranked[:5]}, indent=2, default=lambda x: dict(x) if isinstance(x, Counter) else x))


if __name__ == "__main__": main()
