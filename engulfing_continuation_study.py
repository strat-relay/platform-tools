from __future__ import annotations

import csv, json, math, os, statistics, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import _candle, atr
from simple_sr_candle_validate import levels_before, support_match

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX = "/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY = r"C:\Python39\python.exe"
SYMBOL = "XAUUSDm"
LOOKBACK_DAYS = 184
DOJI_TOLERANCE = 1e-9
HORIZONS = (1, 2, 3, 6)
R_LEVELS = (0.05, 0.10, 0.20, 0.25, 0.50, 0.75, 1.00)


def fetch():
    env = dict(os.environ, WINEPREFIX=PREFIX)
    p = subprocess.run([WINE, WINPY, "Z:" + str(ROOT / "historical_fetch_symbol.py"), SYMBOL], env=env, text=True, capture_output=True, timeout=180, check=True)
    return json.loads(p.stdout.splitlines()[-1])


def v(bar, key): return _candle(bar, key)
def iso(ts): return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
def pct(n, d): return 100.0 * n / d if d else 0.0
def sign_color(bar):
    d = v(bar, "close") - v(bar, "open")
    return "BULLISH" if d > DOJI_TOLERANCE else "BEARISH" if d < -DOJI_TOLERANCE else "DOJI"
def qbucket(values, x):
    if not values: return "UNKNOWN"
    q1, q2 = statistics.quantiles(values, n=3, method="inclusive") if len(values) > 2 else (values[0], values[-1])
    return "LOW" if x <= q1 else "MEDIUM" if x <= q2 else "HIGH"


def bullish_engulfing(bars, i):
    if i < 1: return None
    p, c = bars[i - 1], bars[i]
    po, pc = v(p, "open"), v(p, "close")
    co, cc = v(c, "open"), v(c, "close")
    if not (pc < po and cc > co and co <= pc and cc >= po): return None
    rng = max(v(c, "high") - v(c, "low"), 1e-12)
    body = abs(cc - co)
    return {"previous_body": abs(pc - po), "body": body, "range": rng, "body_atr": None, "body_vs_previous": body / max(abs(pc - po), 1e-12), "close_location": (cc - v(c, "low")) / rng, "upper_wick": v(c, "high") - cc, "lower_wick": co - v(c, "low")}


def metrics(rows, key="net_r"):
    vals = [float(r[key]) for r in rows if r.get(key) not in (None, "")]
    wins = [x for x in vals if x > 0]; losses = [x for x in vals if x < 0]
    eq = peak = dd = 0.0
    for x in vals:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {"count": len(rows), "resolved": len(vals), "wins": len(wins), "losses": len(losses), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "average": statistics.mean(vals) if vals else 0.0, "median": statistics.median(vals) if vals else 0.0, "cumulative": sum(vals), "max_drawdown": dd}


def continuation_probability(rows, condition):
    return pct(sum(condition(r) for r in rows), len(rows))


def build_rows(bars, m15, contract, timeframe):
    end = max(int(x["time"]) for x in bars[:-1]); start = end - LOOKBACK_DAYS * 86400
    work = [x for x in bars if start <= int(x["time"]) < end]
    m15_times = [int(x["time"]) for x in m15]; cache = {}; rows = []
    confirmed_points = None
    for i in range(40, len(work) - 7):
        details = bullish_engulfing(work, i)
        if not details: continue
        t = int(work[i]["time"]); a = atr(work[max(0, i - 40):i + 1])[-1] or 0.0; details["body_atr"] = details["body"] / max(a, 1e-12)
        mi = max(0, __import__("bisect").bisect_left(m15_times, t) - (1 if timeframe == "M15" else 0))
        if mi not in cache: cache[mi] = levels_before(m15[max(0, mi - 480):mi], t, confirmed_points)
        levels = cache[mi]; support = support_match(levels, work[i], t, a)
        spread = float(work[i].get("spread", 0)) * float(contract["point"]); ask = v(work[i], "close") + spread / 2
        buffer = max(a * 0.10, spread * 1.25, float(contract["tick_size"]))
        stop = min(v(work[i], "low"), support["zone_low"] if support else v(work[i], "low")) - buffer
        risk = ask - stop
        if risk <= 0: continue
        nxt = work[i + 1]
        row = {"timeframe": timeframe, "setup_id": f"ECS-{timeframe}-{t}", "timestamp": iso(t), "support_condition": "AT_SUPPORT" if support else "AWAY_FROM_SUPPORT", "support_level": support["center_price"] if support else None, "support_touches": support["touches"] if support else 0, "engulfing_open": v(work[i], "open"), "engulfing_high": v(work[i], "high"), "engulfing_low": v(work[i], "low"), "engulfing_close": v(work[i], "close"), "entry_ask": ask, "structural_stop": stop, "stop_distance": risk, "spread_price": spread, "spread_points": work[i].get("spread", 0), "atr": a, "next_open": v(nxt, "open"), "next_high": v(nxt, "high"), "next_low": v(nxt, "low"), "next_close": v(nxt, "close"), "next_color": sign_color(nxt), "body_atr": details["body_atr"], "body_vs_previous": details["body_vs_previous"], "close_location": details["close_location"], "upper_wick": details["upper_wick"], "lower_wick": details["lower_wick"], "next_net_move_price": (v(nxt, "close") - spread / 2) - ask, "next_raw_move_price": v(nxt, "close") - v(work[i], "close")}
        row["next_net_r"] = row["next_net_move_price"] / risk; row["next_raw_r"] = row["next_raw_move_price"] / risk
        for h in HORIZONS:
            window = work[i + 1:i + 1 + h]
            highs = [v(x, "high") for x in window]; lows = [v(x, "low") for x in window]
            row[f"mfe_{h}"] = (max(highs) - ask) / risk; row[f"mae_{h}"] = (ask - min(lows)) / risk
            row[f"new_high_{h}"] = bool(highs and max(highs) > v(work[i], "high"))
            colors = [sign_color(x) for x in window]
            row[f"majority_bullish_{h}"] = colors.count("BULLISH") > colors.count("BEARISH")
        for r in R_LEVELS:
            row[f"favorable_before_adverse_{r}"] = row["mfe_1"] >= r and row["mae_1"] < r
        rows.append(row)
    return rows, start, end


def main():
    data = fetch(); contract = data["contract"]; all_rows = {}; periods = {}
    for tf in ("M5", "M15", "H1"):
        all_rows[tf], start, end = build_rows(data[tf], data["M15"], contract, tf); periods[tf] = {"start": iso(start), "end": iso(end)}
    summary = {"symbol": SYMBOL, "paper_only": True, "definition": "Frozen SIMPLE_SR_CANDLE_V1 body-only bullish engulfing; no support filter for primary result", "periods": periods, "contract": contract, "timeframes": {}, "support_comparison": {}, "notes": ["Observational study only; no strategy or runner modified.", "MFE/MAE use theoretical ask after engulfing close and bar extremes; net next-candle P/L uses ask entry and bid exit approximation from recorded bar spread."]}
    for tf, rows in all_rows.items():
        next_mfe_price = [r["next_high"] - r["entry_ask"] for r in rows]
        spread_prices = [r["spread_price"] for r in rows]
        summary["timeframes"][tf] = {"engulfings": len(rows), "next_candle": Counter(r["next_color"] for r in rows), "continuation_pct": pct(sum(r["next_color"] == "BULLISH" for r in rows), len(rows)), "majority_2_pct": pct(sum(r["majority_bullish_2"] for r in rows), len(rows)), "majority_3_pct": pct(sum(r["majority_bullish_3"] for r in rows), len(rows)), "new_high_pct": {str(h): pct(sum(r[f"new_high_{h}"] for r in rows), len(rows)) for h in HORIZONS}, "mfe_mae_r": {str(h): {"median_mfe": statistics.median(r[f"mfe_{h}"] for r in rows), "median_mae": statistics.median(r[f"mae_{h}"] for r in rows)} for h in HORIZONS}, "median_next_mfe_price": statistics.median(next_mfe_price), "median_spread_price": statistics.median(spread_prices), "next_mfe_exceeds_spread_pct": pct(sum(m > s for m, s in zip(next_mfe_price, spread_prices)), len(rows)), "next_candle_observation": metrics(rows, "next_net_r"), "r_thresholds_before_adverse": {str(r): pct(sum(x[f"favorable_before_adverse_{r}"] for x in rows), len(rows)) for r in R_LEVELS}}
        summary["support_comparison"][tf] = {k: {"count": len(v), "next_bullish_pct": continuation_probability(v, lambda x: x["next_color"] == "BULLISH"), "median_next_mfe_r": statistics.median(x["mfe_1"] for x in v) if v else 0.0, "median_next_mae_r": statistics.median(x["mae_1"] for x in v) if v else 0.0, "observation": metrics(v, "next_net_r")} for k, v in {"ALL": rows, "AT_SUPPORT": [x for x in rows if x["support_condition"] == "AT_SUPPORT"], "AWAY_FROM_SUPPORT": [x for x in rows if x["support_condition"] == "AWAY_FROM_SUPPORT"]}.items()}
        for field in ("body_atr", "body_vs_previous", "close_location"):
            vals = sorted(float(x[field]) for x in rows); summary["timeframes"][tf].setdefault("strength_buckets", {})[field] = {b: {"count": len(v), "next_bullish_pct": continuation_probability(v, lambda x: x["next_color"] == "BULLISH"), "median_mfe_r": statistics.median(x["mfe_1"] for x in v) if v else 0.0} for b, v in ((b, [x for x in rows if qbucket(vals, float(x[field])) == b]) for b in ("LOW", "MEDIUM", "HIGH"))}
    primary = all_rows["M5"]
    fields = sorted({k for r in primary for k in r})
    with open(ROOT / "engulfing_continuation_samples.csv", "w", newline="", encoding="utf-8") as f: w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(primary)
    (ROOT / "engulfing_continuation_results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = ["# Bullish engulfing continuation study", "", "READ-ONLY historical research on XAUUSDm. No strategy or runner modified.", "", "## Timeframe comparison", "| Timeframe | Engulfings | Next bullish | Continuation % |", "|---|---:|---:|---:|"]
    for tf in ("M5", "M15", "H1"):
        x = summary["timeframes"][tf]; lines.append(f"| {tf} | {x['engulfings']} | {x['next_candle'].get('BULLISH', 0)} | {x['continuation_pct']:.2f}% |")
    lines += ["", "## Support comparison · M5", "| Condition | Count | Next bullish % | Median next MFE (R) | Median next MAE (R) | Net win rate | Net average move (R) |", "|---|---:|---:|---:|---:|---:|---:|"]
    for k, x in summary["support_comparison"]["M5"].items(): lines.append(f"| {k} | {x['count']} | {x['next_bullish_pct']:.2f}% | {x['median_next_mfe_r']:.3f} | {x['median_next_mae_r']:.3f} | {x['observation']['win_rate_pct']:.2f}% | {x['observation']['average']:.4f} |")
    lines += ["", "## M5 short-horizon results", json.dumps(summary["timeframes"]["M5"], indent=2), "", "## Interpretation", "The study measures candle color separately from tradable continuation after spread. It does not create or recommend a strategy."]
    (ROOT / "engulfing_continuation_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"symbol": SYMBOL, "timeframes": summary["timeframes"], "support_comparison_m5": summary["support_comparison"]["M5"]}, indent=2))


if __name__ == "__main__": main()
