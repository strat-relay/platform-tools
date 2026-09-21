from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from .engine import structure_direction
from .neutral import NEUTRAL_CONFIG
from .replay import _context_direction, _raw_trigger
from .state_machine import detect_m15_setups

def h4_from_h1(rows):
    groups={}
    for x in rows:
        t=int(x["time"]); bucket=t-(t%14400); groups.setdefault(bucket,[]).append(x)
    out=[]
    for t, xs in sorted(groups.items()): out.append({"time":t,"open":xs[0]["open"],"high":max(x["high"] for x in xs),"low":min(x["low"] for x in xs),"close":xs[-1]["close"],"spread":xs[-1].get("spread",0)})
    return out

def main():
    d=json.loads(Path("/tmp/usdjpy_historical_20260917.json").read_text()); m5=d["M5"]; m15=d["M15"]; h1=d["H1"]; h4=h4_from_h1(h1)
    start=int(datetime(2026,9,10,7,0,tzinfo=timezone.utc).timestamp()); end=int(datetime(2026,9,10,10,0,tzinfo=timezone.utc).timestamp()); trace=[]
    for i,c in enumerate(m5):
        t=int(c["time"])
        if not start<=t<=end: continue
        decision=t+300; raw=_raw_trigger(m5,i)
        if not raw: continue
        setups=detect_m15_setups([x for x in m15 if int(x["time"])+900<=decision],NEUTRAL_CONFIG["m15"])
        active=[x for x in setups if x["status"]=="SETUP_ACTIVE" and x.get("bos_choch_timestamp",0)+900<=decision and x["direction"]==raw["direction"]]
        setup=active[-1] if active else None
        trace.append({"timestamp":datetime.fromtimestamp(t,timezone.utc).isoformat(),"h4_context":_context_direction(h4,decision,"H4"),"h1_context":_context_direction(h1,decision,"H1"),"m15_setup":setup,"m5_trigger":raw,"entry":raw["entry"] if setup else None,"fill":bool(setup),"external_entry_comparison":{"observed":154.016,"delta":(raw["entry"]-154.016) if setup else None}})
    out={"symbol":"USDJPYm","date":"2026-09-10","source":"/tmp/usdjpy_historical_20260917.json","h4_derivation":"H1 native candles aggregated to UTC 4-hour boundaries; diagnostic only","neutral_config":NEUTRAL_CONFIG,"trace":trace,"available":bool(trace)}
    p=Path("artifacts/research/multitimeframe_liquidity_sniper/sep10_trace.json");p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2,default=str)+"\n");print(json.dumps(out,indent=2,default=str))
if __name__=="__main__": main()
