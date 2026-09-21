"""Read-only USDJPY liquidity-displacement replay and reclaim hypothesis audit.

This file is research-only.  It imports the frozen detector but never changes
its source, runner state, broker routing, or live configuration.
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from liquidity_displacement_entry_forward import configure
import liquidity_displacement_forward as base
DATA = Path("/tmp/usdjpy_historical_20260917.json")
OUT = ROOT / "artifacts" / "audits"
START = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp())
TARGET_R = 1.25


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def body(x):
    return abs(float(x["close"]) - float(x["open"]))


def outcome(row, bars, fill_i):
    entry, stop = float(row["entry"]), float(row["stop"])
    risk = abs(entry - stop); long = row["direction"] == "LONG"
    target = entry + risk * TARGET_R if long else entry - risk * TARGET_R
    highs = [float(x["high"]) for x in bars[fill_i:min(len(bars), fill_i + 25)]]
    lows = [float(x["low"]) for x in bars[fill_i:min(len(bars), fill_i + 25)]]
    mae = (entry - min(lows)) / risk if long else (max(highs) - entry) / risk
    mfe = (max(highs) - entry) / risk if long else (entry - min(lows)) / risk
    for step, x in enumerate(bars[fill_i:min(len(bars), fill_i + 25)]):
        hit_sl = float(x["low"]) <= stop if long else float(x["high"]) >= stop
        hit_tp = float(x["high"]) >= target if long else float(x["low"]) <= target
        if hit_sl or hit_tp:
            px = stop if hit_sl else target
            return {"exit_reason": "STOP" if hit_sl else "TARGET", "exit_price": px,
                    "realized_R": (px-entry)/risk if long else (entry-px)/risk,
                    "duration_minutes": step * 5, "MAE": mae, "MFE": mfe,
                    "target": target}
    x = bars[min(len(bars)-1, fill_i + 24)]; px = float(x["close"])
    return {"exit_reason": "TIME_EXIT", "exit_price": px,
            "realized_R": (px-entry)/risk if long else (entry-px)/risk,
            "duration_minutes": 120, "MAE": mae, "MFE": mfe, "target": target}


def frozen_replay(data, start=START, end=END):
    configure("usdjpy25")
    strategy = base.LiquidityDisplacementStrategy(base.CFG)
    m5, m15, contract = data["M5"], data["M15"], data["contract"]
    m15_times = [int(x["time"]) for x in m15]
    rows = []
    for i in range(40, len(m5) - 25):
        t = int(m5[i]["time"])
        if not start <= t < end:
            continue
        j = bisect.bisect_right(m15_times, t)
        context = m15[max(0, j - 120):j]
        spread = float(m5[i]["spread"]) * float(contract["point"])
        quote = {"bid": float(m5[i]["close"]) - spread / 2, "ask": float(m5[i]["close"]) + spread / 2}
        candidate = strategy.evaluate(context, m5, quote, contract, iso(t + 300), i)
        if not candidate:
            continue
        row = dict(candidate)
        row.update({"timestamp": iso(t), "candidate_timestamp": iso(m5[candidate["displacement_index"]]["time"]), "setup_id": f"FROZEN-{t}-{i}"})
        if candidate.get("status") == "FILLED":
            row.update(outcome({"entry": candidate["entry"], "stop": candidate["stop_loss"], "direction": candidate["direction"]}, m5, int(candidate["fill_index"])))
            row["fill_price"] = candidate["entry"]
        else:
            row.update({"exit_reason": None, "exit_price": None, "realized_R": None, "MAE": None, "MFE": None})
        rows.append(row)
    return rows


def research_candidates(data, start=0, end=None):
    """Generic causal reclaim variant; no screenshot price is referenced."""
    m5, contract = data["M5"], data["contract"]
    end = end or len(m5) - 25
    rows = []
    for i in range(max(40, start), end):
        c = m5[i]; prior = m5[i-36:i]
        level = min(float(x["low"]) for x in prior)
        # A sell-side sweep may close below the prior low; reclaim is a later
        # completed candle.  This is the behavioral difference from frozen V1.
        if not (float(c["low"]) < level and float(c["close"]) <= level):
            continue
        reclaim_i = next((j for j in range(i + 1, min(i + 61, len(m5))) if float(m5[j]["close"]) > level), None)
        if reclaim_i is None:
            continue
        displacement_events = []
        med = statistics.median(body(x) for x in m5[max(0, i-12):i])
        atr_proxy = statistics.mean(float(x["high"]) - float(x["low"]) for x in m5[max(0, i-14):i])
        for j in range(reclaim_i, min(reclaim_i + 61, len(m5))):
            d = m5[j]; rng = float(d["high"]) - float(d["low"]); b = body(d)
            close_loc = (float(d["close"]) - float(d["low"])) / rng if rng else 0
            prior_high = max(float(x["high"]) for x in m5[max(i-5, 0):i])
            if b >= max(.5 * atr_proxy, med) and float(d["close"]) > float(d["open"]) and close_loc >= .6 and float(d["close"]) > prior_high:
                displacement_events.append((j, prior_high))
        if not displacement_events:
            continue
        # Test every mechanically generated .000/.500 level that is reclaimed
        # after the displacement/BOS.  Levels are never selected from a target.
        for disp_i, break_level in displacement_events:
            levels = [x / 2 for x in range(math.floor((float(m5[disp_i]["low"]) - 1) * 2), math.ceil((float(m5[disp_i]["high"]) + 1) * 2) + 1)]
            for key in levels:
              key_reclaim = next((j for j in range(disp_i, min(disp_i + 6, len(m5)))
                                if float(m5[j]["close"]) >= key and (j == 0 or float(m5[j-1]["close"]) < key)), None)
              if key_reclaim is None:
                  continue
              fill_i = next((j for j in range(key_reclaim, min(key_reclaim + 6, len(m5)))
                           if float(m5[j]["low"]) <= key and float(m5[j]["close"]) >= key), None)
              if fill_i is None:
                  continue
              spread = float(m5[fill_i]["spread"]) * float(contract["point"])
              entry = key + spread / 2
              stop = float(c["low"]) - max(.1 * atr_proxy, 1.25 * spread, float(contract["tick_size"]))
              if entry <= stop:
                  continue
              row = {"setup_id": f"RECLAIM-{int(c['time'])}-{i}-{disp_i}-{key:.3f}", "timestamp": iso(m5[key_reclaim]["time"]),
                   "direction": "LONG", "sweep_level": level, "sweep_extreme": c["low"],
                   "sweep_index": i, "reclaim_index": reclaim_i, "displacement_index": disp_i,
                   "break_level": break_level, "key_level": key, "key_reclaim_index": key_reclaim,
                   "entry": entry, "fill_index": fill_i, "fill_price": entry, "stop": stop,
                   "spread_at_fill": spread, "body": body(m5[disp_i]), "atr_proxy": atr_proxy,
                   "status": "FILLED"}
              row.update(outcome(row, m5, fill_i)); rows.append(row)
    # one key-level event per sweep/key combination; later displacement
    # observations are not independent trades.
    unique = {}
    for row in rows:
        unique.setdefault((row["sweep_index"], row["key_level"]), row)
    return list(unique.values())


def metrics(rows):
    rs = [float(x["realized_R"]) for x in rows if x.get("realized_R") is not None]
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]
    eq = peak = dd = 0; streak = longest = 0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
        streak = streak + 1 if r < 0 else 0; longest = max(longest, streak)
    return {"total_candidates": len(rows), "total_fills": sum(x.get("status") == "FILLED" for x in rows),
            "resolved": len(rs), "rejected_or_unfilled": sum(x.get("status") != "FILLED" for x in rows),
            "win_rate_pct": 100 * len(wins) / (len(wins) + len(losses)) if wins or losses else 0,
            "average_R": statistics.mean(rs) if rs else 0, "median_R": statistics.median(rs) if rs else 0,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "expectancy_R": statistics.mean(rs) if rs else 0, "max_consecutive_losses": longest,
            "max_drawdown_R": dd, "average_hold_minutes": statistics.mean([x["duration_minutes"] for x in rows if x.get("duration_minutes") is not None]) if rs else 0,
            "MAE_median_R": statistics.median([x["MAE"] for x in rows if x.get("MAE") is not None]) if any(x.get("MAE") is not None for x in rows) else None,
            "MFE_median_R": statistics.median([x["MFE"] for x in rows if x.get("MFE") is not None]) if any(x.get("MFE") is not None for x in rows) else None}


def write_svg(data, target):
    """Write a dependency-free historical OHLC diagnostic chart."""
    bars = data["M5"][58530:58625]; w, h, left, top, bottom = 1500, 720, 70, 35, 55
    lo = min(float(x["low"]) for x in bars); hi = max(float(x["high"]) for x in bars); pw, ph = w-left-25, h-top-bottom
    def X(i): return left + i * pw / max(1, len(bars)-1)
    def Y(v): return top + (hi-v) * ph / (hi-lo)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             '<rect width="100%" height="100%" fill="#fff"/><text x="70" y="22" font-family="sans-serif" font-size="18">USDJPYm Sep 10 2026 causal replay</text>']
    for i, b in enumerate(bars):
        x=X(i); color="#16803c" if b["close"] >= b["open"] else "#b42318"; y1,y2=Y(float(b["high"])),Y(float(b["low"]))
        yo,yc=Y(float(b["open"])),Y(float(b["close"])); rect_y=min(yo,yc); rect_h=max(1,abs(yc-yo));
        parts += [f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="1"/>',
                  f'<rect x="{x-3:.1f}" y="{rect_y:.1f}" width="6" height="{rect_h:.1f}" fill="{color}"/>']
    if target:
        def line(v, color, dash, label):
            parts.append(f'<line x1="{left}" y1="{Y(v):.1f}" x2="{w-25}" y2="{Y(v):.1f}" stroke="{color}" stroke-dasharray="{dash}"/><text x="{w-260}" y="{Y(v)-4:.1f}" font-family="sans-serif" font-size="12" fill="{color}">{label} {v:.3f}</text>')
        line(float(target["sweep_level"]), "#7c3aed", "6,4", "sweep reference")
        line(float(target["key_level"]), "#2563eb", "6,4", "154.000 key reclaim")
        line(float(target["stop"]), "#dc2626", "2,4", "stop")
        line(float(target["target"]), "#059669", "2,4", "target")
        for idx, label, color, mark in ((target["sweep_index"]-58530,"SWEEP","#7c3aed","circle"),(target["displacement_index"]-58530,"BOS/DISP","#f59e0b","circle"),(target["fill_index"]-58530,"RESEARCH FILL","#2563eb","triangle")):
            if 0 <= idx < len(bars):
                x=X(idx); y=Y(float(bars[idx]["low"] if label == "SWEEP" else (target["entry"] if label == "RESEARCH FILL" else bars[idx]["close"])))
                shape = f'<polygon points="{x:.1f},{y-9:.1f} {x-7:.1f},{y+6:.1f} {x+7:.1f},{y+6:.1f}" fill="{color}"/>' if mark == "triangle" else f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}"/>'
                parts += [shape, f'<text x="{x+8:.1f}" y="{y-8:.1f}" font-family="sans-serif" font-size="12" fill="{color}">{label}</text>']
    parts += [f'<text x="{left}" y="{h-35}" font-family="sans-serif" font-size="12" fill="#b42318">Frozen V1 at 07:30: rejected — sweep candle closed below its reference; no same-event candidate.</text>',
              f'<text x="{left}" y="{h-15}" font-family="sans-serif" font-size="12">UTC: {iso(bars[0]["time"])} to {iso(bars[-1]["time"])}</text></svg>']
    (OUT / "usdjpy_sep10_13_replay.svg").write_text("\n".join(parts), encoding="utf-8")


def main():
    data = json.loads(DATA.read_text())
    frozen_window = frozen_replay(data)
    research_window = [x for x in research_candidates(data) if START <= int(datetime.fromisoformat(x["timestamp"]).timestamp()) < END]
    full_start = int(data["M5"][40]["time"])
    full_end = int(data["M5"][-26]["time"])
    split = full_start + (2 * (full_end - full_start)) // 3
    # Full dataset replay uses the same chronological detector and fixed rules.
    # Keep this local and never call the production validator, which writes its
    # normal result files.
    frozen_full = frozen_replay(data, full_start, full_end)
    research_full = research_candidates(data)
    target = next((x for x in research_window if x["key_level"] == 154.0), None)
    event = {"historical_window": {"start": iso(START), "end": iso(END)},
             "data_coverage": {k: {"bars": len(data[k]), "start": iso(data[k][0]["time"]), "end": iso(data[k][-1]["time"])} for k in ("M5", "M15", "H1")},
             "frozen_window": frozen_window, "research_window": research_window,
             "frozen_metrics_window": metrics(frozen_window), "frozen_metrics_full": metrics(frozen_full),
             "research_metrics_window": metrics(research_window), "research_metrics_full": metrics(research_full),
             "chronological_split": {"split": iso(split),
                                      "frozen_discovery": metrics([x for x in frozen_full if int(datetime.fromisoformat(x["timestamp"]).timestamp()) < split]),
                                      "frozen_validation": metrics([x for x in frozen_full if int(datetime.fromisoformat(x["timestamp"]).timestamp()) >= split]),
                                      "research_discovery": metrics([x for x in research_full if int(datetime.fromisoformat(x["timestamp"]).timestamp()) < split]),
                                      "research_validation": metrics([x for x in research_full if int(datetime.fromisoformat(x["timestamp"]).timestamp()) >= split])},
             "research_by_bucket": {".000": metrics([x for x in research_full if int(round(float(x["key_level"]) * 2)) % 2 == 0]),
                                      ".500": metrics([x for x in research_full if int(round(float(x["key_level"]) * 2)) % 2 == 1])},
             "comparison_event": target}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "usdjpy_reclaim_audit_20260917.json").write_text(json.dumps(event, indent=2, default=str) + "\n")
    write_svg(data, target)
    print(json.dumps(event, indent=2, default=str))


if __name__ == "__main__":
    main()
