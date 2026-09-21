"""Neutral bounded replay and durable decision ledger."""
from __future__ import annotations
import hashlib, json, subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from .config import CONTROL_GROUPS
from .engine import _ts, _range, structure_direction, pip_size
from .entry import evaluate_m5_entry
from .neutral import NEUTRAL_CONFIG
from .state_machine import detect_m15_setups

def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()

def _point(symbol): return .001 if "JPY" in symbol.upper() else .00001

def _atr(rows, i, n=14):
    vals=[_range(x) for x in rows[max(0,i-n):i] if _range(x)>0]
    return sum(vals)/len(vals) if vals else _range(rows[i])

def _raw_trigger(rows, i):
    if i < 1: return None
    c,p=rows[i],rows[i-1]
    if float(c["close"]) > float(c["open"]) and float(c["close"]) > float(p["high"]): d="LONG"
    elif float(c["close"]) < float(c["open"]) and float(c["close"]) < float(p["low"]): d="SHORT"
    else: return None
    return {"trigger_id": f"RAW-M5-{_ts(c['time'])}-{d}", "direction":d, "trigger_timestamp":_ts(c["time"]), "entry":float(c["close"])}

def _context_direction(rows, decision, tf):
    usable=[x for x in rows if _ts(x["time"])+({"H1":3600,"H4":14400}[tf]) <= decision]
    return structure_direction(usable, 5) if usable else None

def _stop(symbol, m5, fill_i, direction, buffer):
    lo=min(float(x["low"]) for x in m5[max(0,fill_i-2):fill_i+1]); hi=max(float(x["high"]) for x in m5[max(0,fill_i-2):fill_i+1]); a=_atr(m5,fill_i)
    return lo-buffer*a if direction=="LONG" else hi+buffer*a

def _outcome(m5, fill_i, entry, stop, direction, target_r, hold_minutes):
    risk=entry-stop if direction=="LONG" else stop-entry
    if risk <= 0: return None
    target=entry+risk*target_r if direction=="LONG" else entry-risk*target_r
    end=min(len(m5), fill_i+max(1,int(hold_minutes/5))); mae=0.0; mfe=0.0
    for i in range(fill_i+1,end):
        c=m5[i]; high=float(c["high"]); low=float(c["low"])
        mae=max(mae, (entry-low)/risk if direction=="LONG" else (high-entry)/risk)
        mfe=max(mfe, (high-entry)/risk if direction=="LONG" else (entry-low)/risk)
        sl=low<=stop if direction=="LONG" else high>=stop; tp=high>=target if direction=="LONG" else low<=target
        if sl or tp: return {"gross_R":-1.0 if sl else target_r, "exit": "STOP" if sl else "TARGET", "mae_R":mae, "mfe_R":mfe, "hold_minutes":(i-fill_i)*5, "target":target}
    c=m5[end-1]; px=float(c["close"]); gross=(px-entry)/risk if direction=="LONG" else (entry-px)/risk
    return {"gross_R":gross,"exit":"TIME","mae_R":mae,"mfe_R":mfe,"hold_minutes":(end-1-fill_i)*5,"target":target}

def _cost(symbol, bar, entry, stop):
    spread_price=float(bar.get("spread",0))*_point(symbol); return {"spread_pips":spread_price/pip_size(symbol), "stop_distance_pips":abs(entry-stop)/pip_size(symbol), "spread_cost_R":spread_price/abs(entry-stop) if entry != stop else None}

def _max_dd(values):
    equity=peak=dd=0.0
    for value in values:
        equity += float(value); peak=max(peak,equity); dd=max(dd,peak-equity)
    return dd

def replay(dataset: dict[str, dict[str, list[dict[str, Any]]]], *, output_dir: str|Path) -> dict[str, Any]:
    outdir=Path(output_dir); outdir.mkdir(parents=True, exist_ok=True); rows=[]; reasons=Counter(); fills=[]
    m15p=NEUTRAL_CONFIG["m15"]; m5p=NEUTRAL_CONFIG["m5"]
    for symbol,data in dataset.items():
        m5=sorted(data["M5"],key=lambda x:_ts(x["time"])); m15=sorted(data["M15"],key=lambda x:_ts(x["time"])); h1=data["H1"]; h4=data["H4"]
        for i in range(1,len(m5)-1):
            decision=_ts(m5[i]["time"])+300; raw=_raw_trigger(m5,i)
            if not raw: continue
            setups=detect_m15_setups([x for x in m15 if _ts(x["time"])+900<=decision],m15p)
            active=[x for x in setups if x["status"]=="SETUP_ACTIVE" and x.get("bos_choch_timestamp",0)+900<=decision and x["direction"]==raw["direction"]]
            setup=active[-1] if active else None
            h1_dir=_context_direction(h1,decision,"H1"); h4_dir=_context_direction(h4,decision,"H4")
            controls={"CONTROL_A_M5_ONLY":True,"CONTROL_B_M15":setup is not None,
                      "CONTROL_C_M15_H1":setup is not None and h1_dir in ({"BULLISH_HH_HL"} if raw["direction"]=="LONG" else {"BEARISH_LH_LL"}),
                      "CONTROL_D_H4_H1_M15":setup is not None and h1_dir in ({"BULLISH_HH_HL"} if raw["direction"]=="LONG" else {"BEARISH_LH_LL"}) and h4_dir in ({"BULLISH_HH_HL"} if raw["direction"]=="LONG" else {"BEARISH_LH_LL"})}
            if setup is None: reasons["M15_SETUP_MISSING_OR_DIRECTION_MISMATCH"]+=1
            elif not controls["CONTROL_C_M15_H1"]: reasons["H1_CONTEXT_MISMATCH"]+=1
            elif not controls["CONTROL_D_H4_H1_M15"]: reasons["H4_CONTEXT_MISMATCH"]+=1
            for group,ok in controls.items():
                if not ok: continue
                # All four controls use the identical completed-candle M5 BOS
                # trigger.  Context only gates eligibility; it never changes
                # the M5 trigger or fill semantics.
                entry_evt={"family":"MICRO_BOS_CHOCH","confirmation_timestamp":raw["trigger_timestamp"],
                           "order_created_timestamp":raw["trigger_timestamp"],"activation_timestamp":raw["trigger_timestamp"],
                           "fill_timestamp":raw["trigger_timestamp"],"fill_price":raw["entry"]}
                row={"setup_id":setup.get("setup_id") if setup else None,"symbol":symbol,"direction":raw["direction"],"decision_timestamp":_iso(decision),
                     "h4_source_timestamp":_iso(max([_ts(x["time"]) for x in h4 if _ts(x["time"])+14400<=decision],default=0)) if h4 else None,
                     "h1_source_timestamp":_iso(max([_ts(x["time"]) for x in h1 if _ts(x["time"])+3600<=decision],default=0)) if h1 else None,
                     "m15_setup":setup,"m5_trigger":raw,"control_level":group,"rejection_reason":None if ok else "CONTEXT_REJECTED","entry":entry_evt}
                if entry_evt and not entry_evt.get("unsupported"):
                    fi=next((j for j,x in enumerate(m5) if _ts(x["time"])==entry_evt["fill_timestamp"]),None)
                    if fi is not None:
                        entry=float(entry_evt["fill_price"]); stop=_stop(symbol,m5,fi,raw["direction"],float(m5p["stop_buffer_atr"])); result=_outcome(m5,fi,entry,stop,raw["direction"],float(m5p["target_r"]),int(m5p["max_hold_minutes"])); cost=_cost(symbol,m5[fi],entry,stop) if result else {}
                        if result:
                            row.update({"fill_timestamp":_iso(entry_evt["fill_timestamp"]),"fill_price":entry,"spread_timestamp":_iso(entry_evt["fill_timestamp"]),"spread_points":m5[fi].get("spread"),"stop":stop,"target":result["target"],**result,**cost,"net_R":result["gross_R"]-(cost.get("spread_cost_R") or 0)})
                            fills.append(row)
                rows.append(row)
    ledger=outdir/"bounded_replay_ledger.jsonl"; ledger.write_text("".join(json.dumps(x,sort_keys=True,default=str)+"\n" for x in rows))
    metrics={}
    for g in CONTROL_GROUPS:
        group_rows=[x for x in rows if x["control_level"]==g]; rs=[float(x["net_R"]) for x in fills if x["control_level"]==g and x.get("net_R") is not None]; gross=[float(x["gross_R"]) for x in fills if x["control_level"]==g]
        metrics[g]={"raw_triggers":len(group_rows),"qualified_setups":sum(bool(x.get("m15_setup")) for x in group_rows),"fills":len(rs),"expired":len(group_rows)-len(rs),"invalidated":0,"gross_expectancy_R":sum(gross)/len(gross) if gross else None,"net_expectancy_R":sum(rs)/len(rs) if rs else None,"fill_time_spread_cost_R":sum(float(x.get("spread_cost_R",0)) for x in fills if x["control_level"]==g)/len(rs) if rs else None,"profit_factor":sum(x for x in rs if x>0)/abs(sum(x for x in rs if x<0)) if any(x<0 for x in rs) else None,"max_DD_R":_max_dd(rs)}
    cost_values=[float(x["spread_cost_R"]) for x in fills if x.get("spread_cost_R") is not None]
    buckets=Counter(("<=0.05R" if x<=.05 else "0.05-0.10R" if x<=.10 else "0.10-0.15R" if x<=.15 else "0.15-0.20R" if x<=.20 else "0.20-0.30R" if x<=.30 else ">0.30R") for x in cost_values)
    report={"family":"MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH","mode":"BOUNDED_VALIDATION_ONLY","config":NEUTRAL_CONFIG,"symbols":sorted(dataset),"metrics":metrics,"context_attrition":dict(reasons),"empirical_spread_cost":{"fills":len(cost_values),"median":sorted(cost_values)[len(cost_values)//2] if cost_values else None,"mean":sum(cost_values)/len(cost_values) if cost_values else None,"buckets":dict(buckets)},"ledger_path":str(ledger),"durable_ledger_pass":ledger.exists()}
    (outdir/"bounded_replay_report.json").write_text(json.dumps(report,indent=2,default=str)+"\n")
    return report
