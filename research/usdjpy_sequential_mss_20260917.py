"""Research-only sequential displacement -> MSS experiment for USDJPYm."""
import bisect, copy, json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_engine import atr
from research.usdjpy_delayed_reclaim_20260917 import (DATA, START, END, iso, body, outcome, metrics,
    frozen_strategy, DelayedReclaimStrategy)

ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"artifacts/audits/usdjpy_sequential_mss_20260917.json"
WINDOWS=(0,1,2,3,5,8); POLICIES={"IMMEDIATE":0,"DELAYED_N1":1,"DELAYED_N2":2}
FROZEN_TEMPLATE=frozen_strategy()

class Sequential:
    def __init__(self,reclaim_n,bos_window):
        self.reclaim_n=reclaim_n; self.bos_window=bos_window
        self.config=type("C",(),{"max_structure_break_candles":5,"min_body_atr":.5,"min_body_median_multiple":1.,"min_close_location":.6,"atr_buffer_fraction":.1})()
        self.levels=FROZEN_TEMPLATE.__class__(copy.deepcopy(FROZEN_TEMPLATE.config))
        self.delayed_control=DelayedReclaimStrategy(reclaim_n) if reclaim_n else None
    def evaluate(self,m15,m5,quote,contract,timestamp,i):
        if self.bos_window==0:
            if self.reclaim_n==0:
                return self.levels.find_candidate(m15,m5,i,quote,contract,timestamp)
            # Delegate the delayed BOS=0 control to the reconciled research
            # harness; do not reimplement delayed reclaim here.
            return self.delayed_control.evaluate(m15,m5,quote,contract,timestamp,i)
        if i<40 or len(m15)<30:return None
        c=m5[i]; a=atr(m5[max(0,i-119):i+1])[-1]; spread=float(quote["ask"])-float(quote["bid"])
        if a<=0 or spread<=0:return None
        low,high,_,_=self.levels._levels(m5,i)
        if low is None or high is None:return None
        long=float(c["low"])<low; short=float(c["high"])>high
        direction="LONG" if long else "SHORT" if short else None
        if not direction:return None
        ref=low if direction=="LONG" else high
        immediate=(float(c["close"])>ref if direction=="LONG" else float(c["close"])<ref)
        if immediate: rec=i
        else:
            rec=next((j for j in range(i+1,min(len(m5),i+1+self.reclaim_n)) if (float(m5[j]["close"])>ref if direction=="LONG" else float(m5[j]["close"])<ref)),None)
        if rec is None:return None
        prior=m5[max(0,i-12):i]; med=sorted(body(x) for x in prior)[len(prior)//2]
        micro=max(float(x["high"]) for x in m5[max(i-5,i-12):i]) if direction=="LONG" else min(float(x["low"]) for x in m5[max(i-5,i-12):i])
        # Preserve the frozen five-candle displacement search, now without
        # requiring BOS on the same candle.
        disp=None
        for j in range(rec+1,min(len(m5),rec+1+5)):
            d=m5[j]; rng=float(d["high"])-float(d["low"]); b=body(d); loc=(float(d["close"])-float(d["low"]))/rng if rng else .5
            bull=direction=="LONG" and float(d["close"])>float(d["open"]) and loc>=.6
            bear=direction=="SHORT" and float(d["close"])<float(d["open"]) and loc<=.4
            if b>=a*.5 and b>=med and (bull or bear): disp=j; break
        if disp is None:return None
        bos=None
        for j in range(disp,min(len(m5),disp+1+self.bos_window)):
            close=float(m5[j]["close"]); ok=close>micro if direction=="LONG" else close<micro
            if ok: bos=j; break
        if bos is None:return None
        d=m5[disp]; dl,dh=float(d["low"]),float(d["high"]); dr=dh-dl
        entry=dh-dr*.25 if direction=="LONG" else dl+dr*.25
        buffer=max(a*.1,spread*1.25,float(contract.get("tick_size",.001))); ext=min(float(m5[k]["low"]) for k in range(i,rec+1)) if direction=="LONG" else max(float(m5[k]["high"]) for k in range(i,rec+1))
        stop=ext-buffer if direction=="LONG" else ext+buffer; risk=entry-stop if direction=="LONG" else stop-entry
        if risk<=0:return None
        return {"direction":direction,"sweep_level":ref,"sweep_extreme":float(c["low"] if direction=="LONG" else c["high"]),"sweep_index":i,"reclaim_index":rec,"reclaim_delay":rec-i,"immediate_reclaim":rec==i,"displacement_index":disp,"bos_index":bos,"bos_delay":bos-disp,"break_level":micro,"entry":entry,"stop_loss":stop,"risk":risk,"atr":a,"spread":spread,"body_atr":body(d)/ (float(d["high"])-float(d["low"]) or 1),"close_location":(float(d["close"])-dl)/(dr or 1),"displacement_range":dr,"max_adverse_extension":abs(ext-ref),"status":"CANDIDATE","entry_type":"RETRACE_0.250000_DISPLACEMENT","setup_type":"LIQUIDITY_DISPLACEMENT_SEQUENTIAL_MSS_RESEARCH_V1","candidate_timestamp":iso(m5[bos]["time"]),"reclaim_timestamp":iso(m5[rec]["time"])}

def replay_seq(s,data,start,end):
    m5,m15,contract=data["M5"],data["M15"],data["contract"]; mt=[x["time"] for x in m15]; rows=[]; seen=set(); i=40
    while i<min(end,len(m5)-25):
        if int(m5[i]["time"])<start:i+=1;continue
        t=int(m5[i]["time"]);j=bisect.bisect_right(mt,t);ctx=m15[max(0,j-120):j];sp=float(m5[i]["spread"])*float(contract["point"]);q={"bid":float(m5[i]["close"])-sp/2,"ask":float(m5[i]["close"])+sp/2}; c=s.evaluate(ctx,m5,q,contract,iso(t+300),i)
        if c:
            key=(c.get("sweep_index",i),c["direction"])
            if key not in seen:
                seen.add(key);r=dict(c);r["timestamp"]=iso(t);r["setup_id"]=f"Sequential-{t}-{i}";start_fill=c.get("bos_index",c.get("displacement_index",i))+1;fi=None
                for k in range(start_fill,min(len(m5),start_fill+5)):
                    b=m5[k];touch=float(b["low"])<=c["entry"] if c["direction"]=="LONG" else float(b["high"])>=c["entry"];held=float(b["close"])>=c["entry"] if c["direction"]=="LONG" else float(b["close"])<=c["entry"]
                    if touch and held:fi=k;break
                r.update({"filled":fi is not None,"fill_index":fi,"fill_price":c["entry"] if fi is not None else None})
                r.update(outcome({"entry":c["entry"],"stop_loss":c["stop_loss"],"direction":c["direction"]},m5,fi) if fi is not None else {"exit_reason":None,"exit_price":None,"realized_R":None,"duration_minutes":None,"MAE":None,"MFE":None,"target":None});rows.append(r)
        i+=1
    return rows

def main():
    d=json.loads(DATA.read_text());start=int(d["M5"][40]["time"]);end=int(d["M5"][-26]["time"]); split=start+2*(end-start)//3
    base=replay_seq(Sequential(0,0),d,start,end);keys={(x["timestamp"],x["direction"]) for x in base};grid={};raw={}; control_rows={"IMMEDIATE":base}; control_keys={"IMMEDIATE":keys}
    for pn,rn in (("DELAYED_N1",1),("DELAYED_N2",2)):
        control_rows[pn]=replay_seq(Sequential(rn,0),d,start,end); control_keys[pn]={(x["timestamp"],x["direction"]) for x in control_rows[pn]}
    for pn,rn in POLICIES.items():
      for w in WINDOWS:
        if w==0: rows=control_rows[pn]
        else:
          extra=replay_seq(Sequential(rn,w),d,start,end)
          extra=[x for x in extra if x.get("bos_delay",0)>0 and (x["timestamp"],x["direction"]) not in control_keys[pn]]
          rows=control_rows[pn]+extra
        raw[f"{pn}|BOS_{w}"]=rows; add=[x for x in rows if (x["timestamp"],x["direction"]) not in control_keys[pn]];am=metrics(add)
        grid[f"{pn}|BOS_{w}"]={"policy":pn,"bos_window":w,"full":metrics(rows),"discovery":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())<split]),"validation":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())>=split]),"incremental":{"added_candidates":len(add),"added_fills":sum(x.get("filled") for x in add),"positive_R":sum((x.get("realized_R") or 0)>0 for x in add),"negative_R":sum((x.get("realized_R") or 0)<0 for x in add),"zero_R":sum(x.get("realized_R") is not None and x.get("realized_R")==0 for x in add),"target_exits":sum(x.get("exit_reason")=="TARGET" for x in add),"stop_exits":sum(x.get("exit_reason")=="STOP" for x in add),"time_exits":sum(x.get("exit_reason")=="TIME_EXIT" for x in add),"expectancy_R":am["expectancy_R"],"PF":am["profit_factor"],"max_DD_R":am["max_drawdown_R"],"max_losing_streak":am["max_losing_streak"]}}
    i=next(k for k,x in enumerate(d["M5"]) if int(x["time"])==int(datetime(2026,9,10,7,30,tzinfo=timezone.utc).timestamp())); sep={};
    for pn,rn in POLICIES.items():
      for w in WINDOWS:
        m=[x for x in raw[f"{pn}|BOS_{w}"] if x["timestamp"]==iso(d["M5"][i]["time"]) and x["direction"]=="LONG"];sep[f"{pn}|BOS_{w}"]=m[0] if m else None
    # Delay buckets from widest N=2 / BOS=8 research population.
    widest=raw["DELAYED_N2|BOS_8"]; buckets={}
    for lab,lo,hi in [("same",0,1),("1",1,2),("2",2,3),("3",3,4),("4-5",4,6),("6-8",6,9)]:
      z=[x for x in widest if lo<=x.get("bos_delay",0)<hi];buckets[lab]=metrics(z)
    result={"baseline":{"events":len(base),"fills":sum(x.get("filled") for x in base),"equivalence_control":"IMMEDIATE|BOS_0"},"grid":grid,"sep10":sep,"sep10_mechanics":{"sweep":"2026-09-10T07:30:00+00:00","reference":153.405,"reclaim":"2026-09-10T07:40:00+00:00","reclaim_delay":2,"displacement":"2026-09-10T08:00:00+00:00","micro":153.553,"first_bos_close":"2026-09-10T08:10:00+00:00","bos_delay_from_displacement":2,"pipeline":"08:10 BOS candle fails displacement; sequential candidate is therefore absent"},"bos_delay_buckets_N2_BOS8":buckets}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(result,indent=2)+"\n");print(json.dumps(result,indent=2))
if __name__=="__main__":main()
