from __future__ import annotations
import csv, json, statistics
from collections import defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASELINE={"expectancy_r":0.0021324830566360007,"max_drawdown_r":16.13788030539925}

def pct(n,d): return 100*n/d if d else 0.0
def metrics(rows):
    rs=[float(x["r"]) for x in rows]; wins=[float(x["r"]) for x in rows if x["outcome"]=="WIN"]; losses=[float(x["r"]) for x in rows if x["outcome"]=="LOSS"]; eq=peak=dd=0; streak=longest=0
    for r in rs:
        eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    return {"trades":len(rows),"retention_pct":pct(len(rows),294),"wins":len(wins),"losses":len(losses),"time_exits":sum(x["outcome"]=="TIME_EXIT" for x in rows),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest}
def monthly(rows):
    return {m:metrics([x for x in rows if x["month"]==m]) for m in sorted(set(x["month"] for x in rows))}
def rows_for(rows, predicate): return [r for r in rows if predicate(r)]
def meaningful_sample(rows):
    month_counts=defaultdict(int)
    for r in rows: month_counts[r["month"]]+=1
    return len(rows)>=80 or (len([m for m,n in month_counts.items() if n>=10])>=4)

def main():
    all_rows=[r for r in csv.DictReader(open(ROOT/"micro_scalp_edge_trades.csv",encoding="utf-8")) if r["target_r"]=="1.25"]
    for r in all_rows: r["month"]=r["timestamp"][:7]
    assert len(all_rows)==294
    singles={
        "A_aligned_H1_M15":lambda r:r["context_alignment"]=="ALIGNED",
        "B_exclude_conflicting":lambda r:r["context_alignment"]!="CONFLICTING",
        "C_high_ATR_only":lambda r:r["atr_bucket"]=="HIGH",
        "D_exclude_medium_ATR":lambda r:r["atr_bucket"]!="MEDIUM",
        "E_exclude_spread_over_15pct":lambda r:r["spread_stop_bucket"]!="\u003e15%",
        "F_exclude_tightest_stop_quartile":lambda r:r["stop_quartile"]!="Q1",
        "G_SHORT_only":lambda r:r["direction"]=="SHORT",
    }
    combos={
        "1_aligned_and_spread_le_15pct":lambda r:r["context_alignment"]=="ALIGNED" and r["spread_stop_bucket"]!="\u003e15%",
        "2_aligned_and_high_ATR":lambda r:r["context_alignment"]=="ALIGNED" and r["atr_bucket"]=="HIGH",
        "3_aligned_high_ATR_spread_le_15pct":lambda r:r["context_alignment"]=="ALIGNED" and r["atr_bucket"]=="HIGH" and r["spread_stop_bucket"]!="\u003e15%",
        "4_high_ATR_exclude_Q1":lambda r:r["atr_bucket"]=="HIGH" and r["stop_quartile"]!="Q1",
        "5_SHORT_aligned":lambda r:r["direction"]=="SHORT" and r["context_alignment"]=="ALIGNED",
        "6_SHORT_aligned_high_ATR":lambda r:r["direction"]=="SHORT" and r["context_alignment"]=="ALIGNED" and r["atr_bucket"]=="HIGH",
    }
    definitions={**singles,**combos}; results={"baseline":{**BASELINE,"trades":294,"retention_pct":100.0},"single_filters":{},"combinations":{},"minimum_sample_rule":{"meaningful":">=80 trades OR trades in >=4 separate months","exploratory_if_below":True}}
    for group,defs in [("single_filters",singles),("combinations",combos)]:
        for name,fn in defs.items():
            kept=rows_for(all_rows,fn); m=metrics(kept); m["monthly"]=monthly(kept); m["months_with_trades"]=len(m["monthly"]); m["months_with_at_least_10_trades"]=sum(v["trades"]>=10 for v in m["monthly"].values()); m["sample_flag"]="MEANINGFUL" if meaningful_sample(kept) else "EXPLORATORY"; m["improves_expectancy_vs_baseline"]=m["expectancy_r"]>BASELINE["expectancy_r"]; m["improves_drawdown_vs_baseline"]=m["max_drawdown_r"]<BASELINE["max_drawdown_r"]; results[group][name]=m
    months=sorted(set(r["month"] for r in all_rows)); discovery_months=months[:4]; validation_months=months[-2:]
    # Discovery-only selection: candidates must satisfy the sample rule and improve both metrics in discovery.
    discovery_candidates=[]
    for name,fn in definitions.items():
        drows=[r for r in all_rows if r["month"] in discovery_months and fn(r)]; m=metrics(drows); month_count=len(set(r["month"] for r in drows))
        if meaningful_sample(drows) and m["expectancy_r"]>BASELINE["expectancy_r"] and m["max_drawdown_r"]<BASELINE["max_drawdown_r"]:
            discovery_candidates.append((m["expectancy_r"],-m["max_drawdown_r"],name))
    discovery_candidates.sort(reverse=True); selected=[x[2] for x in discovery_candidates[:3]]
    walk={"discovery_months":discovery_months,"validation_months":validation_months,"selected_by_discovery_only":selected,"candidates":{}}
    for name in selected:
        fn=definitions[name]; drows=[r for r in all_rows if r["month"] in discovery_months and fn(r)]; vrows=[r for r in all_rows if r["month"] in validation_months and fn(r)]; walk["candidates"][name]={"discovery":{**metrics(drows),"monthly":monthly(drows)},"validation":{**metrics(vrows),"monthly":monthly(vrows)}}
    # Target check only for discovery-selected filters, with the exact same filter tags and entries.
    target_check={}
    for name in selected:
        fn=definitions[name]; target_check[name]={}
        for target in ["1.0","1.25","1.5"]:
            target_rows=[r for r in csv.DictReader(open(ROOT/"micro_scalp_edge_trades.csv",encoding="utf-8")) if r["target_r"]==target and fn({**r,"month":r["timestamp"][:7]})]
            target_check[name][target]=metrics(target_rows)
    results["walk_forward"] = walk; results["selected_filter_target_check"]=target_check
    results["decision"]="A" if selected and all(x["validation"]["expectancy_r"]>0 and x["validation"]["max_drawdown_r"]<BASELINE["max_drawdown_r"] for x in walk["candidates"].values()) else "B" if selected else "C"
    Path(ROOT/"micro_scalp_filter_results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    md=["# MICRO_SCALP predefined-filter audit","","READ-ONLY / PAPER-ONLY. The 294 canonical setups were reused exactly; no signals or strategy logic were changed.","","## Baseline",json.dumps(results["baseline"],indent=2),"","## Single filters",json.dumps(results["single_filters"],indent=2),"","## Specified combinations",json.dumps(results["combinations"],indent=2),"","## Walk-forward","Discovery uses the first four chronological months only; validation uses the final two complete months. Filter selection did not use validation.",json.dumps(results["walk_forward"],indent=2),"","## Target check for discovery-selected filters",json.dumps(target_check,indent=2),"","## Final classification",f"{results['decision']}. Filters were predefined and not exhaustively optimized."]
    (ROOT/"micro_scalp_filter_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"decision":results["decision"],"selected":selected,"single_filters":{k:{x:m[x] for x in ['trades','retention_pct','win_rate_pct','profit_factor','expectancy_r','cumulative_r','max_drawdown_r','longest_losing_streak']} for k,m in results['single_filters'].items()},"walk_forward":walk},indent=2))
if __name__=="__main__": main()
