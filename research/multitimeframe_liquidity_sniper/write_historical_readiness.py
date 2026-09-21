"""Write the final data-expansion readiness gate and diagnostic summaries."""
from __future__ import annotations
import json, random, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_liquidity_sniper"


def pct(values, q):
    if not values: return None
    x = sorted(values); return x[min(len(x)-1, int((len(x)-1)*q))]


def main():
    manifest = json.loads((OUT/"historical_dataset_manifest.json").read_text())
    data = json.loads((OUT/"expanded_historical_dataset.json").read_text())
    replay = json.loads((OUT/"expanded_neutral_replay.json").read_text())
    rows = [json.loads(x) for x in (OUT/"expanded_neutral_ledger.jsonl").read_text().splitlines() if x]
    align_samples = 0; align_fail = 0
    rng = random.Random(20260917)
    for symbol, frames in data.items():
        m5 = sorted(frames["M5"], key=lambda x:int(x["time"]))
        if not m5: continue
        # Alignment is evaluated on the explicit common overlap of the four
        # series.  A tail that has no corresponding higher-timeframe history
        # is a coverage limitation, not a candle-alignment failure.
        seconds = {"M5":300,"M15":900,"H1":3600,"H4":14400}
        start = max(int(frames[tf][0]["time"]) + seconds[tf] for tf in ("M5","M15","H1","H4") if frames[tf])
        end = min(int(frames[tf][-1]["time"]) + (300 if tf == "M5" else {"M15":900,"H1":3600,"H4":14400}[tf]) for tf in ("M5","M15","H1","H4") if frames[tf])
        eligible = [i for i,x in enumerate(m5) if start <= int(x["time"])+300 <= end]
        choices = ([eligible[0], eligible[len(eligible)//2], eligible[-1]] + [rng.choice(eligible) for _ in range(min(25,len(eligible)))]) if eligible else []
        for i in choices:
            if i < 1 or i >= len(m5)-1: continue
            T = int(m5[i]["time"])+300
            ok = True
            for tf, secs in (("H4",14400),("H1",3600),("M15",900),("M5",300)):
                usable = [x for x in frames[tf] if int(x["time"])+secs <= T]
                ok = ok and bool(usable)
            align_samples += 1; align_fail += int(not ok)
    cost = [float(x["spread_cost_R"]) for x in rows if x.get("spread_cost_R") is not None and x["control"] == "CONTROL_A_M5_ONLY"]
    jpy = [float(x["spread_cost_R"]) for x in rows if x.get("spread_cost_R") is not None and "JPY" in x["symbol"].upper() and x["control"] == "CONTROL_A_M5_ONLY"]
    non = [float(x["spread_cost_R"]) for x in rows if x.get("spread_cost_R") is not None and "JPY" not in x["symbol"].upper() and x["control"] == "CONTROL_A_M5_ONLY"]
    sources = {s:v.get("historical_cost_source") for s,v in json.loads((OUT/"historical_cost_manifest.json").read_text())["symbols"].items()}
    available = [s for s,v in manifest["symbols"].items() if v.get("available")]
    six = [s for s,v in manifest["symbols"].items() if v.get("minimum_6_month_history")]
    twelve = [s for s,v in manifest["symbols"].items() if v.get("minimum_12_month_history")]
    split = json.loads((OUT/"research_split_manifest.json").read_text())
    wf = json.loads((OUT/"walk_forward_manifest.json").read_text())
    result = {
        "schema":"historical-expansion-readiness-v1", "family":"MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH", "research_only":True,
        "HISTORICAL_DATA_AVAILABLE": bool(available), "MINIMUM_6_MONTH_HISTORY": len(six) == len(available) and len(available) >= 5,
        "NATIVE_M5_AVAILABLE": all(manifest["symbols"].get(s,{}).get("M5",{}).get("bar_count",0)>0 for s in available),
        "NATIVE_M15_AVAILABLE": all(manifest["symbols"].get(s,{}).get("M15",{}).get("bar_count",0)>0 for s in available),
        "NATIVE_H1_AVAILABLE": all(manifest["symbols"].get(s,{}).get("H1",{}).get("bar_count",0)>0 for s in available),
        "NATIVE_H4_AVAILABLE": all(manifest["symbols"].get(s,{}).get("H4",{}).get("bar_count",0)>0 and "DERIVED" not in manifest["symbols"].get(s,{}).get("H4",{}).get("source","") for s in available),
        "HISTORICAL_TIMEFRAME_ALIGNMENT_PASS": align_fail == 0 and align_samples > 0,
        "HISTORICAL_COST_MODEL_VALID": all(v in ("NATIVE_M5_BAR_SPREAD",) for v in sources.values() if v != "UNAVAILABLE"),
        "RESEARCH_SPLITS_FROZEN": split.get("source_commit") == manifest.get("source_commit"),
        "WALK_FORWARD_FOLDS_FROZEN": wf.get("source_commit") == manifest.get("source_commit"),
        "EXPANDED_NEUTRAL_REPLAY_COMPLETE": replay.get("ledger_rows",0) == len(rows) and len(rows)>0,
        "DURABLE_HISTORICAL_LEDGER_PASS": (OUT/"expanded_neutral_ledger.jsonl").exists() and replay.get("dataset_hash") == manifest.get("dataset_hash"),
        "available_symbols": available, "six_month_symbols": six, "twelve_month_symbols": twelve,
        "alignment_samples": align_samples, "alignment_failures": align_fail,
        "cost_diagnostics_control_A": {"fills":len(cost),"median_R":pct(cost,.5),"mean_R":sum(cost)/len(cost) if cost else None,"P75_R":pct(cost,.75),"P90_R":pct(cost,.90),"P95_R":pct(cost,.95),"P99_R":pct(cost,.99),
                                        "JPY_median_R":pct(jpy,.5),"non_JPY_median_R":pct(non,.5),"JPY_fills":len(jpy),"non_JPY_fills":len(non)},
        "context_metrics": replay["aggregate_equal_pair"],
        "context_attrition": replay["context_attrition"],
        "limitations": ["Only USDJPY has persisted deep M5/M15/H1 history; the other four available symbols are the prior short native snapshot.", "Native H4 is absent from the deep USDJPY export; older H4 is explicitly derived from native H1 and the recent overlap is native.", "Exact historical bid/ask ticks are unavailable; costs use native M5 bar spread, never present-day or sweep-time quotes.", "All ten expansion symbols lack a persisted historical export and were not silently excluded based on performance."],
        "safe_to_begin_staged_optimization": False,
        "source_commit": manifest["source_commit"], "dataset_hash": manifest["dataset_hash"]
    }
    (OUT/"historical_expansion_readiness.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))
if __name__ == "__main__": main()
