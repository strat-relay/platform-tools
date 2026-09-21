from __future__ import annotations
import bisect, csv, json, os, statistics, subprocess
from datetime import datetime, timezone
from pathlib import Path
from liquidity_displacement import LiquidityDisplacementStrategy, LiquidityDisplacementConfig
from liquidity_displacement_validate import iso, pct, session, trend

ROOT=Path(__file__).resolve().parent; WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"; PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"; WINPY=r"C:\Python39\python.exe"; VARIANTS={"USDJPYm_25":("USDJPYm",.25),"XAUUSDm_33":("XAUUSDm",1/3)}; TARGET=1.25; RETRACE=5

def fetch(symbol):
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_symbol_m1.py"),symbol],env=env,text=True,capture_output=True,timeout=180,check=True); return json.loads(p.stdout.splitlines()[-1])

def metrics(rows):
    resolved=[x for x in rows if x.get("r") is not None]; rs=[float(x["r"]) for x in resolved]; wins=[x for x in resolved if x["outcome"]=="WIN"]; losses=[x for x in resolved if x["outcome"]=="LOSS"]; eq=peak=dd=0.; streak=longest=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    gp,gl=sum(float(x["r"]) for x in wins),sum(float(x["r"]) for x in losses); dur=[x["duration_minutes"] for x in resolved]
    return {"setups":len(rows),"filled":sum(x["status"]=="FILLED" for x in rows),"unfilled":sum(x["status"]=="UNFILLED" for x in rows),"wins":len(wins),"losses":len(losses),"time_exits":sum(x["outcome"]=="TIME_EXIT" for x in resolved),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":gp/abs(gl) if gl else None,"expectancy_r":sum(rs)/len(rs) if rs else 0.,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest,"average_duration_minutes":statistics.mean(dur) if dur else 0.,"median_duration_minutes":statistics.median(dur) if dur else 0.}

def conservative(row,bars,fill_i):
    e=float(row["entry"]); sl=float(row["stop_loss"]); risk=abs(e-sl); long=row["direction"]=="LONG"; tp=e+risk*TARGET if long else e-risk*TARGET
    for step in range(25):
        bar=bars[fill_i+step]; hit_sl=float(bar["low"])<=sl if long else float(bar["high"])>=sl; hit_tp=float(bar["high"])>=tp if long else float(bar["low"])<=tp
        if step>=24:
            px=float(bar["close"]); return {"outcome":"TIME_EXIT","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":step*5}
        if hit_sl or hit_tp:
            px=sl if hit_sl else tp; return {"outcome":"LOSS" if hit_sl else "WIN","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":step*5}
    return {"outcome":"INVALID_AFTER_SIGNAL","r":None,"duration_minutes":120}

def m1_settle(row,m5,m1_by_time,fill_i):
    e=float(row["entry"]); sl=float(row["stop_loss"]); risk=abs(e-sl); long=row["direction"]=="LONG"; tp=e+risk*TARGET; tp=e+risk*TARGET if long else e-risk*TARGET; entered=False; ambiguous=[]; missing=False; entry_time=None
    for step in range(25):
        bar=m5[fill_i+step]; bt=int(bar["time"]); minutes=m1_by_time.get(bt,[])
        if not minutes: missing=True; continue
        for minute in minutes:
            low,high=float(minute["low"]),float(minute["high"])
            touch_entry=(low<=e if long else high>=e)
            hit_sl=(low<=sl if long else high>=sl); hit_tp=(high>=tp if long else low<=tp)
            if not entered and touch_entry:
                entered=True; entry_time=int(minute["time"])
                if hit_sl or hit_tp:
                    ambiguous.append({"m5_time":bt,"m1_time":int(minute["time"]),"entry":True,"stop":hit_sl,"target":hit_tp})
                    px=sl if hit_sl else tp; return {"outcome":"LOSS" if hit_sl else "WIN","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":max(0,(int(minute["time"])-entry_time)//60),"m1_resolved":True,"ambiguous_m1":ambiguous,"m1_data_missing":missing}
                continue
            if entered and (hit_sl or hit_tp):
                if hit_sl and hit_tp: ambiguous.append({"m5_time":bt,"m1_time":int(minute["time"]),"entry":False,"stop":True,"target":True})
                px=sl if hit_sl else tp; return {"outcome":"LOSS" if hit_sl else "WIN","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":max(0,(int(minute["time"])-entry_time)//60),"m1_resolved":True,"ambiguous_m1":ambiguous,"m1_data_missing":missing}
    if entered:
        last=int(m5[min(fill_i+24,len(m5)-1)]["time"])+299; px=float(m5[min(fill_i+24,len(m5)-1)]["close"]); return {"outcome":"TIME_EXIT","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":max(0,(last-entry_time)//60),"m1_resolved":True,"ambiguous_m1":ambiguous,"m1_data_missing":missing}
    return {"outcome":"INVALID_AFTER_SIGNAL","r":None,"duration_minutes":0,"m1_resolved":False,"ambiguous_m1":ambiguous,"m1_data_missing":missing}

def run(name,symbol,fraction):
    data=fetch(symbol); m5=data["M5"]; m1=data["M1"]; end=max(int(x["time"]) for x in m5[:-1]); start=end-184*86400; split=start+((end-start)*2)//3; bars=[x for x in m5 if start<=int(x["time"])<end]; m15=data["M15"]; mt=[int(x["time"]) for x in m15]; c=data["contract"]; m1_by_time={}
    for x in m1: m1_by_time.setdefault(int(x["time"])-int(x["time"])%60,[]).append(x)
    strategy=LiquidityDisplacementStrategy(LiquidityDisplacementConfig(symbol=symbol,target_r=TARGET,max_hold_minutes=120,max_retrace_candles=RETRACE,max_structure_break_candles=5,min_body_atr=.5,min_body_median_multiple=1.,min_close_location=.6,atr_buffer_fraction=.1)); rows=[]
    for i in range(40,len(bars)-25):
        t=int(bars[i]["time"]); j=bisect.bisect_right(mt,t); ctx=m15[max(0,j-120):j]; spread=float(bars[i]["spread"])*c["point"]; q={"bid":float(bars[i]["close"])-spread/2,"ask":float(bars[i]["close"])+spread/2}; cand=strategy.find_candidate(ctx,bars,i,q,c,iso(t+300))
        if not cand: continue
        d=bars[cand["displacement_index"]]; lo,hi=float(d["low"]),float(d["high"]); entry=hi-(hi-lo)*fraction if cand["direction"]=="LONG" else lo+(hi-lo)*fraction; stop=float(cand["stop_loss"]); fill_i=None
        for fi in range(cand["displacement_index"]+1,min(len(bars),cand["displacement_index"]+1+RETRACE)):
            b=bars[fi]; touched=float(b["low"])<=entry if cand["direction"]=="LONG" else float(b["high"])>=entry; held=float(b["close"])>=entry if cand["direction"]=="LONG" else float(b["close"])<=entry
            if touched and held: fill_i=fi; break
        row={"setup_id":f"{name}-{t}-{i}-{cand['direction']}","timestamp":iso(t+300),"timestamp_epoch":t+300,"period":"DISCOVERY" if t<split else "VALIDATION","month":datetime.fromtimestamp(t,timezone.utc).strftime("%Y-%m"),"direction":cand["direction"],"entry":entry,"stop_loss":stop,"stop_distance":abs(entry-stop),"status":"FILLED" if fill_i is not None else "UNFILLED","fill_index":fill_i,"same_m5_entry_and_exit":False,"m1_available":False,"m1_resolution":None}
        if fill_i is not None:
            b=bars[fill_i]; long=cand["direction"]=="LONG"; risk=abs(entry-stop); tp=entry+risk*TARGET if long else entry-risk*TARGET; row["same_m5_entry_and_exit"]=(float(b["low"])<=entry if long else float(b["high"])>=entry) and ((float(b["low"])<=stop if long else float(b["high"])>=stop) or (float(b["high"])>=tp if long else float(b["low"])<=tp));
            cons=conservative(row,bars,fill_i); m1_result=m1_settle(row,bars,m1_by_time,fill_i)
            row.update({"conservative_outcome":cons["outcome"],"conservative_r":cons["r"],"conservative_duration_minutes":cons["duration_minutes"],"m1_available":not m1_result.get("m1_data_missing",False),"m1_resolution":bool(m1_result.get("m1_resolved")),"m1_data_missing":bool(m1_result.get("m1_data_missing",False)),"ambiguous_m1":m1_result.get("ambiguous_m1",[])})
            if m1_result.get("m1_resolved") and not m1_result.get("m1_data_missing"):
                row.update({"outcome":m1_result["outcome"],"r":m1_result["r"],"duration_minutes":m1_result["duration_minutes"],"outcome_source":"M1_SEQUENCE"})
            else:
                row.update({"outcome":cons["outcome"],"r":cons["r"],"duration_minutes":cons["duration_minutes"],"outcome_source":"M5_CONSERVATIVE_FALLBACK"})
        else: row.update({"outcome":"UNFILLED","r":None,"duration_minutes":0})
        rows.append(row)
    discovery=[x for x in rows if x["period"]=="DISCOVERY"]; validation=[x for x in rows if x["period"]=="VALIDATION"]; ambiguous=[x for x in rows if x.get("same_m5_entry_and_exit")]; resolved_m1=[x for x in rows if x.get("m1_resolution")]; m1_ambiguous=[x for x in rows if x.get("ambiguous_m1")]
    same_m5_filled=[x for x in ambiguous if x.get("status")=="FILLED"]; changed=[x for x in same_m5_filled if x.get("outcome_source") and x.get("outcome") != x.get("conservative_outcome")]
    result={"variant":name,"symbol":symbol,"fraction":fraction,"target_r":TARGET,"max_retrace_candles":RETRACE,"period":{"start":iso(start),"split":iso(split),"end":iso(end)},"m1_coverage":{"start":iso(min(int(x["time"]) for x in m1)) if m1 else None,"end":iso(max(int(x["time"]) for x in m1)) if m1 else None,"bars":len(m1)},"discovery":metrics(discovery),"validation":metrics(validation),"all":metrics(rows),"candle_audit":{"same_m5_entry_and_exit_count":len(same_m5_filled),"m1_resolved_filled_count":len(resolved_m1),"m1_ambiguous_sequence_count":len(m1_ambiguous),"m1_missing_for_some_bars":sum(bool(x.get("m1_data_missing")) for x in resolved_m1),"same_m5_outcome_changed_by_m1_count":len(changed),"conservative_sl_priority_applied_if_m1_bar_ambiguous":True}}
    with (ROOT/f"liquidity_displacement_{name}_oos_trades.csv").open("w",newline="") as f: fields=sorted({key for row in rows for key in row}); w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    return result

def main():
    out={}
    for name,(symbol,fraction) in VARIANTS.items():
        try: out[name]=run(name,symbol,fraction)
        except Exception as exc: out[name]={"variant":name,"symbol":symbol,"error":str(exc)}
        print(json.dumps(out[name],indent=2),flush=True)
    (ROOT/"liquidity_displacement_oos_results.json").write_text(json.dumps(out,indent=2)); lines=["# Frozen entry-depth chronological validation","","READ-ONLY / PAPER-ONLY. USDJPY 25% and XAUUSD 33% were frozen before this split.","","| Variant | Discovery PF | Discovery Exp R | Validation PF | Validation Exp R | Validation fills | Validation DD |", "|---|---:|---:|---:|---:|---:|---:|"]
    for n,r in out.items():
        if "error" in r: lines.append(f"| {n} | error | error | error | error | error | error |")
        else: lines.append(f"| {n} | {r['discovery']['profit_factor']} | {r['discovery']['expectancy_r']:.3f} | {r['validation']['profit_factor']} | {r['validation']['expectancy_r']:.3f} | {r['validation']['filled']} | {r['validation']['max_drawdown_r']:.2f}R |")
    (ROOT/"liquidity_displacement_oos_summary.md").write_text("\n".join(lines))
if __name__=="__main__": main()
