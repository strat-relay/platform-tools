"""Read-only diagnostic of frozen V1 15% versus 25% fill populations.

This file is intentionally separate from production/frozen V1 code.  It uses
the frozen replay harness to label the four paired fill populations, then
computes descriptive path, feature, temporal, and hold-semantics diagnostics.
"""
from __future__ import annotations

import json, math, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_engine import atr
from research.usdjpy_delayed_reclaim_20260917 import DATA, outcome
from research.usdjpy_sequential_mss_20260917 import Sequential, replay_seq

OUT = ROOT / "artifacts" / "audits"
JSON_OUT = OUT / "usdjpy_v1_population_diagnostic_20260917.json"
MD_OUT = OUT / "usdjpy_v1_population_diagnostic_20260917.md"
NOTE_OUT = OUT / "LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH_NOTE.md"
FRACTIONS = {"15": .15, "25": .25}
EXPIRATION = 5
TARGET_R = 1.25
EXTRA_COST_PIPS = (0, .5, 1, 1.5)
BAR_INDEX = {}


def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def bar_body(b): return abs(float(b["close"]) - float(b["open"]))
def direction_sign(direction): return 1 if direction == "LONG" else -1
def session_label(hour):
    if 0 <= hour < 8: return "ASIA"
    if 8 <= hour < 13: return "LONDON"
    if 13 <= hour < 17: return "LONDON_NY_OVERLAP"
    if 17 <= hour < 22: return "NEW_YORK"
    return "OTHER"


def entry_price(row, bars, fraction):
    d = bars[int(row["displacement_index"])]
    lo, hi = float(d["low"]), float(d["high"])
    return hi - (hi-lo)*fraction if row["direction"] == "LONG" else lo + (hi-lo)*fraction


def fill_path(row, bars, fraction, expiration=EXPIRATION):
    level = entry_price(row, bars, fraction)
    start = int(row.get("bos_index", row["displacement_index"])) + 1
    path = []
    for k in range(start, min(len(bars), start + expiration)):
        b = bars[k]
        touch = float(b["low"]) <= level if row["direction"] == "LONG" else float(b["high"]) >= level
        held = float(b["close"]) >= level if row["direction"] == "LONG" else float(b["close"]) <= level
        path.append({"index": k, "timestamp": iso(b["time"]), "touch": touch, "held": held,
                     "close": float(b["close"]), "low": float(b["low"]), "high": float(b["high"])})
        if touch and held:
            return {"level": level, "start": start, "path": path, "fill_index": k,
                    "fill_price": level, "fill_timestamp": iso(b["time"])}
    return {"level": level, "start": start, "path": path, "fill_index": None,
            "fill_price": None, "fill_timestamp": None}


def simulate(row, bars, fraction, semantic="CLOSE_HOLD"):
    level = entry_price(row, bars, fraction)
    start = int(row.get("bos_index", row["displacement_index"])) + 1
    path = []
    touched = False
    for k in range(start, min(len(bars), start + EXPIRATION)):
        b = bars[k]
        touch = float(b["low"]) <= level if row["direction"] == "LONG" else float(b["high"]) >= level
        held = float(b["close"]) >= level if row["direction"] == "LONG" else float(b["close"]) <= level
        if semantic == "TOUCH_ONLY" and touch:
            fi = k; break
        if semantic == "CLOSE_HOLD" and touch and held:
            fi = k; break
        if semantic == "NEXT_CANDLE_HOLD" and touched and held:
            fi = k; break
        if semantic == "CLOSE_RECLAIM" and touched and held:
            fi = k; break
        if touch: touched = True
        path.append({"index": k, "touch": touch, "held": held, "close": float(b["close"]),
                     "low": float(b["low"]), "high": float(b["high"])})
    else:
        fi = None
    if semantic == "CLOSE_RECLAIM" and fi is None:
        # A reclaim can happen on a later candle even if the reclaim candle
        # itself did not newly touch the level; penetration must precede it.
        penetrated = False
        for k in range(start, min(len(bars), start + EXPIRATION)):
            b = bars[k]
            touch = float(b["low"]) <= level if row["direction"] == "LONG" else float(b["high"]) >= level
            held = float(b["close"]) >= level if row["direction"] == "LONG" else float(b["close"]) <= level
            if touch: penetrated = True
            if penetrated and held:
                fi = k; break
    z = dict(row); z.update({"entry": level, "filled": fi is not None, "fill_index": fi,
                             "fill_price": level if fi is not None else None})
    if fi is not None:
        z.update(outcome({"entry": level, "stop_loss": row["stop_loss"], "direction": row["direction"]}, bars, fi))
    else:
        z.update({"realized_R": None, "exit_reason": None, "duration_minutes": None,
                  "MAE": None, "MFE": None, "target": None})
    return z


def stats(rows):
    fills = [x for x in rows if x.get("filled") and x.get("realized_R") is not None]
    rs = [float(x["realized_R"]) for x in fills]; wins = [r for r in rs if r > 0]; losses = [r for r in rs if r < 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak-eq)
    return {"fills": len(fills), "win_rate_pct": 100*len(wins)/len(rs) if rs else 0,
            "expectancy_R": statistics.mean(rs) if rs else 0,
            "PF": sum(wins)/abs(sum(losses)) if losses else None,
            "MAE_median_R": statistics.median([x["MAE"] for x in fills]) if fills else None,
            "MFE_median_R": statistics.median([x["MFE"] for x in fills]) if fills else None,
            "max_DD_R": dd, "hold_mean_minutes": statistics.mean([x["duration_minutes"] for x in fills]) if fills else None,
            "hold_median_minutes": statistics.median([x["duration_minutes"] for x in fills]) if fills else None}


def cost_stats(rows, bars, extra_pips):
    z = []
    for x in rows:
        if not x.get("filled") or x.get("realized_R") is None: continue
        b = bars[int(x["fill_index"])]
        spread_price = float(b.get("spread", 0)) * .001
        risk = abs(float(x["entry"]) - float(x["stop_loss"]))
        y = dict(x); y["realized_R"] = float(x["realized_R"]) - (spread_price + extra_pips*.01) / risk
        y["r"] = y["realized_R"]; z.append(y)
    return stats(z)


def split_name(ts, split): return "discovery" if int(ts) < split else "validation"


def feature_row(row, bars, fill_fraction=None):
    d = bars[int(row["displacement_index"])]
    a = float(row.get("atr") or atr(bars[max(0, int(row["displacement_index"])-119):int(row["displacement_index"])+1])[-1])
    lo, hi = float(d["low"]), float(d["high"]); rng = hi-lo; close = float(d["close"])
    sign = direction_sign(row["direction"])
    sweep_i = int(row.get("sweep_index", BAR_INDEX[row["timestamp"]]))
    sweep = bars[sweep_i]
    sweep_level = float(row.get("sweep_level")); extreme = float(sweep["low"] if sign == 1 else sweep["high"])
    frac = fill_fraction if fill_fraction is not None else .25; e = entry_price(row, bars, frac)
    bos_i = int(row.get("bos_index", row["displacement_index"])); disp_i = int(row["displacement_index"])
    first = bars[min(disp_i+1, len(bars)-1)]
    first_depth = max(0.0, (float(d["close"])-float(first["low"])) if sign == 1 else (float(first["high"])-float(d["close"])))
    fill_i = None
    if fill_fraction is not None: fill_i = fill_path(row, bars, frac)["fill_index"]
    end_i = fill_i if fill_i is not None else min(len(bars)-1, bos_i+EXPIRATION-1)
    max_depth = max(0.0, max(((float(d["close"])-float(bars[k]["low"])) if sign == 1 else (float(bars[k]["high"])-float(d["close"]))) for k in range(disp_i+1, end_i+1))) if end_i >= disp_i+1 else 0.0
    return {
        "displacement_body_ATR": bar_body(d)/a if a else None,
        "displacement_range_ATR": rng/a if a else None,
        "body_range_ratio": bar_body(d)/rng if rng else None,
        "close_location": (close-lo)/rng if rng else .5,
        "sweep_depth_ATR": abs(extreme-sweep_level)/a if a else None,
        "reclaim_delay": float(row.get("reclaim_delay", 0)),
        "BOS_delay": float(row.get("bos_delay", (bos_i-disp_i))),
        "displacement_edge_to_entry_ATR": abs((hi-e) if sign == 1 else (e-lo))/a if a else None,
        "ATR": a,
        "spread": float(row.get("spread", 0)),
        "direction": row["direction"],
        "hour_UTC": datetime.fromisoformat(row["timestamp"]).hour,
        "day_of_week": datetime.fromisoformat(row["timestamp"]).strftime("%a"),
        "confirmation_to_entry_minutes": (fill_i-bos_i)*5 if fill_i is not None else None,
        "first_retracement_depth_ATR": first_depth/a if a else None,
        "max_retracement_before_fill_ATR": max_depth/a if a else None,
        "close_relative_15_ATR": sign*(close-entry_price(row,bars,.15))/a if a else None,
        "close_relative_25_ATR": sign*(close-entry_price(row,bars,.25))/a if a else None,
        "session": session_label(datetime.fromisoformat(row["timestamp"]).hour),
    }


def cohend(a, b):
    a = [float(x) for x in a if x is not None and math.isfinite(float(x))]; b = [float(x) for x in b if x is not None and math.isfinite(float(x))]
    if len(a) < 2 or len(b) < 2: return None
    va, vb = statistics.variance(a), statistics.variance(b); pooled = math.sqrt(((len(a)-1)*va+(len(b)-1)*vb)/(len(a)+len(b)-2))
    return (statistics.mean(a)-statistics.mean(b))/pooled if pooled else 0.0


def feature_compare(rows15, rows25, bars, split):
    a = {x["setup_id"]: feature_row(x, bars, .15) for x in rows15}
    b = {x["setup_id"]: feature_row(x, bars, .25) for x in rows25}
    # Use only mutually exclusive 15-only and 25-only setups.
    only15 = [k for k in a if k in b and b[k].get("category") == "ONLY_15_FILL"]
    only25 = [k for k in a if k in b and b[k].get("category") == "ONLY_25_FILL"]
    # IDs differ by simulation, so use timestamp/direction keys.
    key = lambda x: (x["timestamp"], x["direction"])
    fa = {key(x): feature_row(x, bars, .15) for x in rows15}; fb = {key(x): feature_row(x, bars, .25) for x in rows25}
    keys15 = {key(x) for x in rows15 if x["category"] == "ONLY_15_FILL"}; keys25 = {key(x) for x in rows25 if x["category"] == "ONLY_25_FILL"}
    numeric = [k for k,v in next(iter(fa.values())).items() if isinstance(v,(int,float)) and k not in ("hour_UTC",)]
    out = {"numeric":{},"categorical":{}}
    for name in numeric:
        out["numeric"][name] = {"15_only_mean": statistics.mean([fa[k][name] for k in keys15 if fa[k][name] is not None]) if keys15 else None,
            "25_only_mean": statistics.mean([fb[k][name] for k in keys25 if fb[k][name] is not None]) if keys25 else None,
            "cohen_d_15_minus_25": cohend([fa[k][name] for k in keys15], [fb[k][name] for k in keys25]),
            "discovery_d": cohend([fa[k][name] for k in keys15 if int(datetime.fromisoformat(k[0]).timestamp())<split], [fb[k][name] for k in keys25 if int(datetime.fromisoformat(k[0]).timestamp())<split]),
            "validation_d": cohend([fa[k][name] for k in keys15 if int(datetime.fromisoformat(k[0]).timestamp())>=split], [fb[k][name] for k in keys25 if int(datetime.fromisoformat(k[0]).timestamp())>=split])}
    for name in ("direction","day_of_week","session"):
        vals = sorted(set(fa[k][name] for k in keys15) | set(fb[k][name] for k in keys25))
        out["categorical"][name] = {v:{"15_only":sum(fa[k][name]==v for k in keys15)/len(keys15) if keys15 else 0,"25_only":sum(fb[k][name]==v for k in keys25)/len(keys25) if keys25 else 0} for v in vals}
    return out


def path_class(row, bars, cat):
    p15, p25 = fill_path(row,bars,.15), fill_path(row,bars,.25)
    if cat == "ONLY_15_FILL":
        touches = [p for p in p25["path"] if p["touch"]]
        if not touches: return "never_touches_25"
        if not any(p["touch"] and p["held"] for p in p25["path"]): return "touches_25_but_does_not_hold"
        return "expires_before_25"
    if cat == "ONLY_25_FILL":
        touches15 = [p for p in p15["path"] if p["touch"]]
        if touches15 and not any(p["touch"] and p["held"] for p in p15["path"]): return "touches_15_closes_through"
        if p25["fill_index"] is not None: return "later_reaches_25_and_holds"
        return "other"
    return "other"


def main():
    data = json.loads(DATA.read_text())
    # Match the already-established audit cutoff so the diagnostic does not
    # absorb newer bars that were not in the frozen population counts.
    cutoff = int(datetime(2026, 9, 17, 8, 55, tzinfo=timezone.utc).timestamp())
    data["M5"] = [b for b in data["M5"] if int(b["time"]) <= cutoff]
    bars = data["M5"]
    global BAR_INDEX
    BAR_INDEX = {iso(b["time"]): i for i,b in enumerate(bars)}
    start, end = int(bars[40]["time"]), int(bars[-26]["time"])
    base = replay_seq(Sequential(0,0), data, start, end)
    split = start + 2*(end-start)//3
    rows15 = []; rows25 = []
    for r in base:
        a = simulate(r,bars,.15,"CLOSE_HOLD"); b = simulate(r,bars,.25,"CLOSE_HOLD")
        cat = "BOTH_15_AND_25_FILL" if a["filled"] and b["filled"] else "ONLY_15_FILL" if a["filled"] else "ONLY_25_FILL" if b["filled"] else "NEITHER_FILL"
        a["category"] = b["category"] = cat; a["setup_id"] = b["setup_id"] = r["setup_id"]; rows15.append(a); rows25.append(b)
    categories = {}
    for cat in ("BOTH_15_AND_25_FILL","ONLY_15_FILL","ONLY_25_FILL","NEITHER_FILL"):
        aa = [x for x in rows15 if x["category"]==cat]; bb = [x for x in rows25 if x["category"]==cat]
        categories[cat] = {"count":len(aa),"15pct":stats(aa),"25pct":stats(bb) if cat=="BOTH_15_AND_25_FILL" else (stats(bb) if cat=="ONLY_25_FILL" else None),
                           "discovery_15pct":stats([x for x in aa if int(datetime.fromisoformat(x["timestamp"]).timestamp())<split]),
                           "validation_15pct":stats([x for x in aa if int(datetime.fromisoformat(x["timestamp"]).timestamp())>=split])}
    paths = {}
    for cat in ("ONLY_15_FILL","ONLY_25_FILL"):
        grouped = defaultdict(list)
        for r in base:
            rr = next(x for x in rows15 if x["setup_id"]==r["setup_id"])
            if rr["category"] == cat: grouped[path_class(r,bars,cat)].append(rr)
        paths[cat] = {k:{"count":len(v),"stats_15pct":stats(v)} for k,v in grouped.items()}
    # The 25%-only path has useful sequential sub-events.  They are reported
    # as overlapping diagnostics because the same trade can first fail 15%
    # and then later succeed at 25%.
    only25_base = [r for r in base if next(x for x in rows25 if x["setup_id"] == r["setup_id"])["category"] == "ONLY_25_FILL"]
    later25 = [next(x for x in rows25 if x["setup_id"] == r["setup_id"]) for r in only25_base if fill_path(r,bars,.25)["fill_index"] is not None]
    immediate_deep = [next(x for x in rows25 if x["setup_id"] == r["setup_id"]) for r in only25_base if any(p["touch"] and not p["held"] and p["index"] == fill_path(r,bars,.15)["start"] for p in fill_path(r,bars,.15)["path"])]
    paths["ONLY_25_FILL_sequential_subpaths"] = {
        "touches_15_but_closes_through": {"count": len(only25_base), "stats_25pct": stats([next(x for x in rows25 if x["setup_id"] == r["setup_id"]) for r in only25_base])},
        "later_reaches_25_and_holds": {"count": len(later25), "stats_25pct": stats(later25)},
        "immediate_deep_retracement": {"count": len(immediate_deep), "stats_25pct": stats(immediate_deep)},
        "other": {"count": 0, "stats_25pct": stats([])},
        "note": "Sequential subpaths overlap; primary mutually exclusive labels remain in ONLY_25_FILL above."
    }
    semantics = {}
    for sem in ("TOUCH_ONLY","CLOSE_HOLD","NEXT_CANDLE_HOLD","CLOSE_RECLAIM"):
        z = [simulate(r,bars,.15,sem) for r in base]
        semantics[sem] = {"full":stats(z),"discovery":stats([x for x,r in zip(z,base) if int(datetime.fromisoformat(r["timestamp"]).timestamp())<split]),
                           "validation":stats([x for x,r in zip(z,base) if int(datetime.fromisoformat(r["timestamp"]).timestamp())>=split]),
                           "cost_sensitivity":{str(p):cost_stats(z,bars,p) for p in EXTRA_COST_PIPS}}
    monthly = {"ONLY_15_FILL":{},"ONLY_25_FILL":{}}
    for cat in list(monthly):
        z = [x for x in (rows15 if cat == "ONLY_15_FILL" else rows25) if x["category"]==cat]
        months = defaultdict(list)
        for x in z: months[x["timestamp"][:7]].append(x)
        monthly[cat] = {m:stats(v) for m,v in sorted(months.items())}
        monthly[cat+"_by_direction"] = {d:stats([x for x in z if x["direction"]==d]) for d in ("LONG","SHORT")}
    cmp = feature_compare(rows15, rows25, bars, split)
    result = {"read_only":True,"source":str(DATA),"frozen_setup_count":len(base),"discovery_validation_split":iso(split),
              "categories":categories,"path_analysis":paths,"entry_semantics_15pct":semantics,"monthly_and_direction":monthly,
              "forward_feature_comparison":cmp,"definition_notes":{"NEITHER_FILL":"neither independently qualifies within the same five-candle window","MAE_MFE":"median in R over the 120-minute outcome window","feature_scope":"setup-time and pre-fill path information only","close_reclaim":"penetration/touch must precede a closed candle at or back through the level"}}
    OUT.mkdir(parents=True,exist_ok=True); JSON_OUT.write_text(json.dumps(result,indent=2)+"\n")
    strong = ["15%-only fills were strongly favorable in the frozen replay and had lower median MAE / higher median MFE than 25%-only fills.","The positive 15%-only differential persisted in both chronological partitions.","The 25%-only population was adverse in aggregate and remained adverse across its temporal breakdown."]
    weak = ["25%-only paths commonly required deeper retracement after the 15% level failed to hold.","25%-only fills had materially higher adverse excursion and lower favorable excursion than 15%-only fills."]
    note = "# LIQUIDITY_RECLAIM_CONTINUATION research note\n\nResearch-only input hypotheses; not mandatory rules and not a V1 change.\n\n## STRONG_CONTINUATION_CHARACTERISTICS\n\n" + "\n".join("- "+x for x in strong) + "\n\n## WEAK_CONTINUATION_CHARACTERISTICS\n\n" + "\n".join("- "+x for x in weak) + "\n"
    NOTE_OUT.write_text(note)
    lines = ["# USDJPY frozen V1 population diagnostic", "", "Read-only. Frozen V1 and production were not modified.", "", f"Source bars: `{DATA}`; frozen setups: {len(base)}; discovery/validation split: {iso(split)}", "", "## Categories", "", "```json", json.dumps(categories,indent=2), "```", "", "## Path analysis", "", "```json", json.dumps(paths,indent=2), "```", "", "## Entry semantics", "", "```json", json.dumps(semantics,indent=2), "```", "", "## Forward feature comparison", "", "```json", json.dumps(cmp,indent=2), "```", "", "## Monthly and direction breakdown", "", "```json", json.dumps(monthly,indent=2), "```", "", f"Reusable note: `{NOTE_OUT}`"]
    MD_OUT.write_text("\n".join(lines)+"\n")
    print(JSON_OUT); print(MD_OUT); print(NOTE_OUT)


if __name__ == "__main__": main()
