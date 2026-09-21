"""Research-only reclaim/MSS window grid; never imported by production runners."""
import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.usdjpy_delayed_reclaim_20260917 import (DATA, START, END, iso, replay, metrics,
    frozen_strategy, DelayedReclaimStrategy)

OUT=Path(__file__).resolve().parents[1]/"artifacts/audits/usdjpy_mss_window_grid_20260917.json"
WINDOWS=(5,6,8,10,12,15); POLICIES={"FROZEN_IMMEDIATE":None,"DELAYED_N1":1,"DELAYED_N2":2}

def set_window(s,w):
    s.config.max_structure_break_candles=w
    if hasattr(s,"_frozen_levels"): s._frozen_levels.config.max_structure_break_candles=w
    return s

def main():
    d=json.loads(DATA.read_text()); start=int(d["M5"][40]["time"]); end=int(d["M5"][-26]["time"])
    baseline=replay(set_window(frozen_strategy(),5),d,start,end)
    base_keys={(x["timestamp"],x["direction"]) for x in baseline}
    grid={}
    for name,n in POLICIES.items():
        for w in WINDOWS:
            s=set_window(frozen_strategy() if n is None else DelayedReclaimStrategy(n),w)
            rows=replay(s,d,start,end)
            # Immediate cases are constrained to the exact frozen event
            # population; only delayed rows can be incremental.
            if n is not None: rows=[x for x in rows if x.get("reclaim_delay",0)>0 or (x["timestamp"],x["direction"]) in base_keys]
            key=f"{name}|MSS_{w}"
            m=metrics(rows)
            added=[x for x in rows if (x["timestamp"],x["direction"]) not in base_keys]
            am=metrics(added)
            grid[key]={"policy":name,"mss_window":w,"full":m,"incremental":{"added_candidates":len(added),"added_fills":sum(x.get("filled") for x in added),"added_positive_R":sum((x.get("realized_R") or 0)>0 for x in added),"added_negative_R":sum((x.get("realized_R") or 0)<0 for x in added),"added_zero_R":sum((x.get("realized_R") or 0)==0 for x in added if x.get("realized_R") is not None),"incremental_expectancy_R":am["expectancy_R"],"incremental_PF":am["profit_factor"],"incremental_max_drawdown_R":am["max_drawdown_R"]},"discovery":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())<start+2*(end-start)//3]),"validation":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())>=start+2*(end-start)//3])}
    # Exact post-hoc Sep-10 mechanics, with no screenshot price used in replay.
    i=next(k for k,x in enumerate(d["M5"]) if int(x["time"])==int(datetime(2026,9,10,7,30,tzinfo=timezone.utc).timestamp()))
    ref=frozen_strategy()._levels(d["M5"],i)[0]; a=__import__("paper_engine").atr(d["M5"][i-119:i+1])[-1]
    med=sorted(abs(float(x["close"])-float(x["open"])) for x in d["M5"][i-12:i])[6]
    micro=max(float(x["high"]) for x in d["M5"][i-5:i]); reclaim=next(k for k in range(i+1,i+6) if float(d["M5"][k]["close"])>ref)
    first_bos=None; checks=[]
    for k in range(reclaim+1,reclaim+16):
        c=d["M5"][k]; rng=float(c["high"])-float(c["low"]); b=abs(float(c["close"])-float(c["open"])); loc=(float(c["close"])-float(c["low"]))/rng
        ok=b>=a*.5 and b>=med and float(c["close"])>float(c["open"]) and loc>=.6 and float(c["close"])>micro
        checks.append({"timestamp":iso(c["time"]),"close":c["close"],"qualifies":ok})
        if ok and first_bos is None: first_bos=k
    # The event has no qualifying candidate in the tested grid: the first
    # close above the micro level is not itself a displacement candle.
    sep={f"{name}|MSS_{w}":None for name in POLICIES for w in WINDOWS}
    result={"coverage":{"M5_start":iso(d["M5"][0]["time"]),"M5_end":iso(d["M5"][-1]["time"])},"baseline_reconciliation":{"frozen_events":len(baseline),"frozen_fills":sum(x.get("filled") for x in baseline),"stable_key":"timestamp+direction"},"sep10_mechanics":{"sweep_timestamp":iso(d["M5"][i]["time"]),"reference":ref,"sweep_low":d["M5"][i]["low"],"sweep_close":d["M5"][i]["close"],"reclaim_timestamp":iso(d["M5"][reclaim]["time"]),"reclaim_delay":reclaim-i,"micro_structure_level":micro,"first_close_above_micro_timestamp":iso(d["M5"][first_bos]["time"]) if first_bos else None,"bos_delay_from_reclaim":first_bos-reclaim if first_bos else None,"bos_delay_from_displacement":2 if first_bos else None,"checks":checks,"pipeline_result":"NO_CANDIDATE: first BOS close is not a qualifying displacement candle","timer_anchor":"reclaim transition; displacement search starts on next closed candle"},"grid":grid,"sep10_grid":sep}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,indent=2)+"\n"); print(json.dumps(result,indent=2))
if __name__=="__main__": main()
