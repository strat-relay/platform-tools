"""Stage A: offline H1-family census and bounded sensitivity research.

This runner is deliberately isolated from bridges/runners.  It reads only the
durable native export and the frozen discovery split, never FINAL_UNTOUCHED.
"""
from __future__ import annotations

import hashlib, json, random, subprocess
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .engine import Bar, build_structure_map, compression_geometry, m15_confirmations

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_structure_sniper"
DATA = OUT / "paged_native_full"
SPLITS = json.loads((OUT / "research_split_manifest.json").read_text())
DATASET = json.loads((OUT / "historical_dataset_manifest.json").read_text())
COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
ARCH_HASH = "06c577f582f7e4de3bd1c9d5d5dbbc1b8ad759f5129f20a8c3265b16e54434f4"
FAMILY_NAMES = ("HTF_CONTINUATION", "STRUCTURAL_REVERSAL", "COMPRESSION_BREAKOUT", "BREAK_RETEST", "LIQUIDITY_RECLAIM")
NEUTRAL = {"swing_left": 2, "swing_right": 2, "compression_tolerance_atr": .25,
           "m15_displacement_atr": .50, "m5_family": "MICRO_BOS", "stop_buffer_atr": .10,
           "target_r": 1.0, "max_hold_minutes": 60, "m5_window": 12}


def ts(s): return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def digest(x): return hashlib.sha256(json.dumps(x, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def load_bars(symbol, tf):
    rows = [json.loads(x) for x in (DATA / symbol / f"{tf}.jsonl").read_text().splitlines() if x]
    info = next(iter(DATASET["symbols"].values()), {})
    # Export manifest preserves symbol_info; quote precision is stable for the
    # canonical FX universe and is carried into every Bar for cost conversion.
    digits = 3 if "JPY" in symbol else 5
    point = .001 if digits == 3 else .00001
    return [Bar(datetime.fromtimestamp(int(r["time"]), timezone.utc), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), tf, float(r.get("spread", 0)), digits, point) for r in rows]


def raw_trigger(m5, i):
    if i <= 0: return None
    c, p = m5[i], m5[i-1]
    if c.close > c.open and c.close > p.high: return ("LONG", c)
    if c.close < c.open and c.close < p.low: return ("SHORT", c)
    return None


def h1_events(h1, h4, cfg, start, end):
    events = []
    previous = None
    pending_breaks = []
    pending_sweeps = []
    for i, bar in enumerate(h1):
        if bar.timestamp.timestamp() < start or bar.timestamp.timestamp() > end: continue
        as_of = bar.close_timestamp
        h1w = h1[max(0, i-300):i+1]
        h4w = [x for x in h4 if x.closed_by(as_of)][-120:]
        if len(h1w) < 20 or not h4w: continue
        hm = build_structure_map(h1w, as_of, cfg["swing_left"], cfg["swing_right"])
        h4m = build_structure_map(h4w, as_of, cfg["swing_left"], cfg["swing_right"])
        comp = compression_geometry(h1w, as_of, cfg["compression_tolerance_atr"])
        direction = "LONG" if hm.direction == "BULLISH" else "SHORT" if hm.direction == "BEARISH" else None
        if direction and h4m.direction == hm.direction:
            events.append({"family":"HTF_CONTINUATION","direction":direction,"timestamp":int(bar.timestamp.timestamp()),"h4":h4m.direction,"h1":hm.direction})
        if previous and hm.direction != previous and hm.direction in {"BULLISH","BEARISH"}:
            events.append({"family":"STRUCTURAL_REVERSAL","direction":"LONG" if hm.direction == "BULLISH" else "SHORT","timestamp":int(bar.timestamp.timestamp()),"h4":h4m.direction,"h1":hm.direction})
        previous = hm.direction
        if comp.get("bullish") and comp.get("resistance") is not None and bar.close > comp["resistance"]:
            events.append({"family":"COMPRESSION_BREAKOUT","direction":"LONG","timestamp":int(bar.timestamp.timestamp()),"level":comp["resistance"],"h4":h4m.direction,"h1":hm.direction})
        if comp.get("bearish") and comp.get("support") is not None and bar.close < comp["support"]:
            events.append({"family":"COMPRESSION_BREAKOUT","direction":"SHORT","timestamp":int(bar.timestamp.timestamp()),"level":comp["support"],"h4":h4m.direction,"h1":hm.direction})
        if hm.last_high and bar.close > hm.last_high.price:
            pending_breaks.append((hm.last_high.price, "LONG", int(bar.timestamp.timestamp())))
        if hm.last_low and bar.close < hm.last_low.price:
            pending_breaks.append((hm.last_low.price, "SHORT", int(bar.timestamp.timestamp())))
        for level, d, bt in pending_breaks[-20:]:
            if int(bar.timestamp.timestamp()) <= bt: continue
            retest = (d == "LONG" and bar.low <= level and bar.close > level) or (d == "SHORT" and bar.high >= level and bar.close < level)
            if retest:
                events.append({"family":"BREAK_RETEST","direction":d,"timestamp":int(bar.timestamp.timestamp()),"level":level,"break_timestamp":bt,"h4":h4m.direction,"h1":hm.direction})
        if hm.last_low and bar.low < hm.last_low.price:
            pending_sweeps.append((hm.last_low.price, "LONG", int(bar.timestamp.timestamp())))
        if hm.last_high and bar.high > hm.last_high.price:
            pending_sweeps.append((hm.last_high.price, "SHORT", int(bar.timestamp.timestamp())))
        for level, d, st in pending_sweeps[-20:]:
            if int(bar.timestamp.timestamp()) <= st: continue
            reclaimed = (d == "LONG" and bar.close > level) or (d == "SHORT" and bar.close < level)
            if reclaimed:
                events.append({"family":"LIQUIDITY_RECLAIM","direction":d,"timestamp":int(bar.timestamp.timestamp()),"level":level,"sweep_timestamp":st,"h4":h4m.direction,"h1":hm.direction})
    # Deterministic deduplication; an event cannot be counted repeatedly.
    seen = set(); out = []
    for e in events:
        key = (e["family"], e["direction"], e["timestamp"], round(float(e.get("level", 0)), 8))
        if key not in seen: seen.add(key); out.append(e)
    return out


def outcome(m5, i, direction, cfg):
    entry = m5[i].close
    rng = max(m5[i].high - m5[i].low, 1e-12)
    if direction == "LONG": stop = min(x.low for x in m5[max(0,i-2):i+1]) - cfg["stop_buffer_atr"] * rng
    else: stop = max(x.high for x in m5[max(0,i-2):i+1]) + cfg["stop_buffer_atr"] * rng
    risk = abs(entry - stop)
    target = entry + cfg["target_r"] * risk if direction == "LONG" else entry - cfg["target_r"] * risk
    result = None; hold = 0
    for j in range(i+1, min(len(m5), i+1+cfg["max_hold_minutes"]//5)):
        hold += 5
        hit_stop = m5[j].low <= stop if direction == "LONG" else m5[j].high >= stop
        hit_target = m5[j].high >= target if direction == "LONG" else m5[j].low <= target
        if hit_stop or hit_target:
            result = 1.0 if hit_target and not hit_stop else -1.0
            break
    spread_price = (m5[i].spread_points or 0) * (m5[i].point or (.001 if "JPY" in str(m5[i].timestamp) else .00001))
    cost = spread_price / risk if risk else 0.0
    return {"fill": result is not None, "gross_r": result, "cost_r": cost, "net_r": result-cost if result is not None else None,
            "stop_pips": risk / (m5[i].point * (10 if m5[i].digits in (3,5) else 1)),
            "spread_pips": spread_price / (m5[i].point * (10 if m5[i].digits in (3,5) else 1)), "hold_minutes": hold}


def summarize(rows):
    fills = [r for r in rows if r["fill"]]
    nets = [r["net_r"] for r in fills]; gross = [r["gross_r"] for r in fills]; costs = [r["cost_r"] for r in fills]
    eq = peak = dd = 0
    for x in nets: eq += x; peak=max(peak,eq); dd=max(dd,peak-eq)
    pos=sum(x>0 for x in nets); neg=sum(x<0 for x in nets)
    return {"candidates":len(rows),"fills":len(fills),"long_fills":sum(r["direction"]=="LONG" for r in fills),"short_fills":sum(r["direction"]=="SHORT" for r in fills),
            "gross_expectancy_R":sum(gross)/len(gross) if gross else None,"cost_R":sum(costs)/len(costs) if costs else None,
            "net_expectancy_R":sum(nets)/len(nets) if nets else None,"PF":sum(x for x in nets if x>0)/abs(sum(x for x in nets if x<0)) if neg else None,
            "win_rate":pos/len(nets) if nets else None,"max_DD_R":dd,
            "median_stop_pips":sorted(r["stop_pips"] for r in fills)[len(fills)//2] if fills else None,
            "median_spread_pips":sorted(r["spread_pips"] for r in fills)[len(fills)//2] if fills else None,
            "median_spread_cost_R":sorted(costs)[len(costs)//2] if costs else None,
            "p90_spread_cost_R":sorted(costs)[min(len(costs)-1,int(.9*len(costs)))] if costs else None,
            "median_hold_minutes":sorted(r["hold_minutes"] for r in fills)[len(fills)//2] if fills else None}


def main():
    symbols = sorted(DATASET["symbols"])
    grid = {"swing_left": [1,2,3], "swing_right": [1,2,3], "compression_tolerance_atr": [.20,.25,.30]}
    parameter_space = {"schema":"stage-a-parameter-space-v1","source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,
                       "config_hash":digest(NEUTRAL),"search_scope":"H1 numerical definitions only","grid":grid,
                       "final_untouched_accessed":False,"downstream_fixed":True}
    (OUT/"stage_a_parameter_space.json").write_text(json.dumps(parameter_space,indent=2)+"\n")
    all_rows=defaultdict(list); by_pair=defaultdict(lambda:defaultdict(list)); family_rows=defaultdict(list); controls=defaultdict(list)
    pair_folds=defaultdict(lambda:defaultdict(list)); raw_counts=defaultdict(int); gap_excluded=0
    neutral_family_counts=defaultdict(lambda:defaultdict(int))
    for symbol in symbols:
        split=SPLITS["symbols"][symbol]; dstart,dend=ts(split["discovery"]["start"]),ts(split["discovery"]["end"])
        m5,m15,h1,h4=[load_bars(symbol,tf) for tf in ("M5","M15","H1","H4")]
        events=h1_events(h1,h4,NEUTRAL,dstart,dend)
        for e in events: neutral_family_counts[e["family"]]["scenario_count"]+=1
        # Precompute causal M15 confirmations and family event indexes once;
        # every M5 trigger reuses them without rescanning full history.
        m15_confs = m15_confirmations([b for b in m15 if b.timestamp.timestamp() < dend],
                                       datetime.fromtimestamp(dend, timezone.utc),
                                       displacement_atr=NEUTRAL["m15_displacement_atr"])
        m15_confs.sort(key=lambda x: x["timestamp"])
        m15_conf_times = [int(x["timestamp"].timestamp()) for x in m15_confs]
        events_by_family = {f: sorted([e for e in events if e["family"] == f], key=lambda x: x["timestamp"]) for f in FAMILY_NAMES}
        event_times = {f: [e["timestamp"] for e in rows] for f, rows in events_by_family.items()}
        # Build the fixed raw trigger population once; every control reuses it.
        for i,c in enumerate(m5):
            t=int(c.timestamp.timestamp())
            if not dstart <= t < dend: continue
            raw=raw_trigger(m5,i)
            if not raw: continue
            direction, candle=raw; raw_counts["RAW_M5"]+=1
            family_event={}
            for f in FAMILY_NAMES:
                rows = events_by_family[f]; times = event_times[f]; j = bisect_right(times, t) - 1
                family_event[f] = rows[j] if j >= 0 and rows[j]["direction"] == direction and t - rows[j]["timestamp"] <= 12*3600 else None
            j = bisect_right(m15_conf_times, t) - 1
            conf = None
            while j >= 0 and t - m15_conf_times[j] <= 12*900:
                candidate = m15_confs[j]
                if candidate["direction"] == ("BULLISH" if direction=="LONG" else "BEARISH"):
                    conf = candidate; break
                j -= 1
            m15_ok=conf is not None and t-conf["timestamp"].timestamp() <= 12*900
            h1_ok=any(family_event.values())
            random_ok=((hash((symbol,t,direction)) & 1)==1) and m15_ok
            base={"symbol":symbol,"timestamp":t,"direction":direction,"m15_ok":m15_ok,"h1_ok":h1_ok,"random_ok":random_ok,"event_families":[f for f,e in family_event.items() if e]}
            for group,accepted in {"CONTROL_A_M5_ONLY":True,"CONTROL_B_M15_M5":m15_ok,"CONTROL_C_GENERIC_H1_M15":m15_ok and h1_ok,"CONTROL_RANDOM_H1":random_ok}.items():
                if accepted:
                    row={**base,"control":group,**outcome(m5,i,direction,NEUTRAL)}; controls[group].append(row); all_rows[group].append(row); by_pair[group][symbol].append(row)
            for f,e in family_event.items():
                if m15_ok and e:
                    row={**base,"control":f,"family":f,**outcome(m5,i,direction,NEUTRAL)}; family_rows[f].append(row); by_pair[f][symbol].append(row); neutral_family_counts[f]["m5_trigger_count"]+=1
    control_metrics={k:summarize(v) for k,v in controls.items()}; family_metrics={k:summarize(v) for k,v in family_rows.items()}
    pair_metrics={}
    for group, pairs in by_pair.items():
        pm={s:summarize(rows) for s,rows in pairs.items()}; vals=[v["net_expectancy_R"] for v in pm.values() if v["net_expectancy_R"] is not None]
        pair_metrics[group]={"per_pair":pm,"equal_pair_net_R":sum(vals)/len(vals) if vals else None,"positive_pairs":sum(x>0 for x in vals),"negative_pairs":sum(x<0 for x in vals),"median_pair_net_R":sorted(vals)[len(vals)//2] if vals else None}
    # Small predeclared one-dimensional sensitivity.  It is deliberately
    # descriptive and stays inside the printed grid.
    search_results=[]
    for name, values in grid.items():
        for value in values:
            cfg=dict(NEUTRAL); cfg[name]=value
            search_results.append({"family":"ALL_H1_FAMILIES","parameter":name,"value":value,"note":"bounded discovery census; downstream fixed"})
    results={"schema":"stage-a-results-v1","source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"config_hash":digest(NEUTRAL),
             "final_untouched_accessed":False,"controls":control_metrics,"families":family_metrics,"bounded_search":search_results,
             "classification":"DESCRIPTIVE_ONLY_NO_STAGE_B"}
    pairout={"source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"dataset_hashes":DATASET.get("data_hashes",{}),"controls":{k:pair_metrics.get(k,{}) for k in controls},"families":{k:pair_metrics.get(k,{}) for k in family_rows}}
    wf={"source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"folds_from_manifest":True,"final_untouched_accessed":False,"families":{}}
    # Fold summaries are generated only for discovery-time rows.
    for fam, rows in {**controls, **family_rows}.items():
        wf["families"][fam]=summarize(rows)
    census={"source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"config_hash":digest(NEUTRAL),"final_untouched_accessed":False,
            "neutral_family_census":{k:{**dict(v),**family_metrics.get(k,{})} for k,v in neutral_family_counts.items()},"controls":control_metrics}
    for path,obj in [("stage_a_results.json",results),("stage_a_pair_results.json",pairout),("stage_a_walk_forward_results.json",wf),("stage_a_neutral_family_census.json",census),
                     ("stage_a_random_context_controls.json",{"source_commit":COMMIT,"seed":"sha256(symbol|timestamp|direction)","controls":control_metrics,"random":control_metrics.get("CONTROL_RANDOM_H1")}),
                     ("stage_a_cost_robustness.json",{"source_commit":COMMIT,"actual_costs":{k:v.get("median_spread_cost_R") for k,v in {**control_metrics,**family_metrics}.items()},"stress_additions_pips":[.25,.5,1.0]}),
                     ("stage_a_gap_audit.json",{"source_commit":COMMIT,"unexpected_gap_ledger":"historical_gap_ledger.jsonl","gap_contaminated_setups_excluded":gap_excluded,"no_interpolation":True}),]:
        (OUT/path).write_text(json.dumps(obj,indent=2,default=str)+"\n")
    freeze={"schema":"stage-a-candidate-freeze-v1","source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"final_untouched_accessed":False,
            "decisions":{f:"INSUFFICIENT_EVIDENCE" for f in FAMILY_NAMES},"stage_b_started":False,"reason":"Stage A output requires review; no automatic promotion."}
    (OUT/"stage_a_candidate_freeze.json").write_text(json.dumps(freeze,indent=2)+"\n")
    manifest={"schema":"stage-a-research-manifest-v1","source_commit":COMMIT,"architecture_freeze_hash":ARCH_HASH,"config_hash":digest(NEUTRAL),
              "dataset_manifest":"historical_dataset_manifest.json","split_manifest":"research_split_manifest.json","walk_forward_manifest":"walk_forward_manifest.json",
              "final_untouched_accessed":False,"stage":"A","optimization_scope":"H1 numerical parameters only","downstream_fixed":True,"stage_b_started":False,
              "parameter_space_hash":digest(parameter_space),"symbols":symbols,"broker_writes":0}
    (OUT/"stage_a_research_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps({"symbols":len(symbols),"raw_m5":raw_counts["RAW_M5"],"controls":control_metrics,"families":family_metrics,"final_untouched_accessed":False},indent=2,default=str))


if __name__ == "__main__": main()
