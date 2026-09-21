from __future__ import annotations
import csv, json, os, statistics, subprocess, bisect
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import atr, ema

ROOT=Path(__file__).resolve().parent
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY=r"C:\Python39\python.exe"
TARGETS=[1.0,1.25,1.5,2.0]

def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX)
    p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch.py")],env=env,text=True,capture_output=True,check=True)
    return json.loads(p.stdout.splitlines()[-1])
def iso(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def pct(n,d): return 100*n/d if d else 0.0
def q(xs,p):
    if not xs:return None
    return statistics.quantiles(xs,n=100,method="inclusive")[max(0,min(98,int(p*100)-1))] if len(xs)>1 else xs[0]
def session(t):
    h=datetime.fromtimestamp(t,timezone.utc).hour
    if 13<=h<16:return "LONDON_NEW_YORK_OVERLAP"
    if h<8:return "ASIA"
    if h<13:return "LONDON"
    if h<21:return "NEW_YORK"
    return "OTHER"

def trend(candles):
    if len(candles)<50:return "RANGING"
    closes=[float(x["close"]) for x in candles]
    fast,slow=ema(closes,20)[-1],ema(closes,50)[-1]
    a=atr(candles)[-1]
    if a<=0 or abs(fast-slow)<=0.10*a:return "RANGING"
    return "BULLISH" if fast>slow else "BEARISH"

def outcome(row, bars, index, target_r):
    entry=float(row["entry"]); stop=float(row["stop_loss"]); risk=abs(entry-stop); long=row["direction"]=="LONG"
    target=entry+risk*target_r if long else entry-risk*target_r
    mae=0.0; mfe=0.0; duration=0; result="TIME_EXIT"; exit_price=None; exit_time=None
    # Signal candle has already closed. Replay only subsequent completed M5 candles.
    for step in range(1,7):
        j=index+step
        if j>=len(bars): result="INVALID_AFTER_SIGNAL"; break
        c=bars[j]; high=float(c["high"]); low=float(c["low"]); now=int(c["time"])+300
        adverse=(entry-low) if long else (high-entry)
        favorable=(high-entry) if long else (entry-low)
        mae=max(mae,adverse/risk); mfe=max(mfe,favorable/risk); duration=step*5
        if step>=6:
            exit_price=float(c["close"]); exit_time=now; result="TIME_EXIT"; break
        hit_sl=low<=stop if long else high>=stop
        hit_tp=high>=target if long else low<=target
        if hit_sl or hit_tp:
            # Existing paper execution rule: SL wins when both occur in one candle.
            exit_price=stop if hit_sl else target; exit_time=now; result="LOSS" if hit_sl else "WIN"; break
    if exit_price is None:
        return {"outcome":result,"r":None,"mae_r":mae,"mfe_r":mfe,"duration_minutes":duration,"exit_price":None,"exit_time":exit_time}
    realized=((exit_price-entry)/risk) if long else ((entry-exit_price)/risk)
    return {"outcome":result,"r":realized,"mae_r":mae,"mfe_r":mfe,"duration_minutes":duration,"exit_price":exit_price,"exit_time":exit_time}

def summarize(rows, key="segment"):
    rs=[x["r"] for x in rows if x["r"] is not None]; wins=[x["r"] for x in rows if x["outcome"]=="WIN"]; losses=[x["r"] for x in rows if x["outcome"]=="LOSS"]; eq=peak=dd=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
    return {"trades":len(rs),"resolved_trades":len(rs),"wins":len(wins),"losses":len(losses),"time_exits":sum(x["outcome"]=="TIME_EXIT" for x in rows),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"median_r":statistics.median(rs) if rs else 0,"average_r":sum(rs)/len(rs) if rs else 0,"cumulative_r":sum(rs),"max_drawdown_r":dd,"average_duration_minutes":sum(x["duration_minutes"] for x in rows)/len(rows) if rows else 0,"median_duration_minutes":statistics.median([x["duration_minutes"] for x in rows]) if rows else 0}

def segment_report(rows, field):
    out={}
    for value in sorted(set(x[field] for x in rows)):
        group=[x for x in rows if x[field]==value]; s=summarize(group); out[str(value)]=s
    return out

def main():
    canonical=list(csv.DictReader(open(ROOT/"micro_scalp_canonical_setups.csv",encoding="utf-8")))
    data=fetch(); bars=data["M5"]; times=[int(x["time"]) for x in bars]
    # Reuse canonical setup fields and enrich with contextual descriptors only.
    enriched=[]
    atr_values=[]; stop_values=[]
    for row in canonical:
        t=int(float(row["timestamp_epoch"])); idx=bisect.bisect_left(times,t-300)
        m5=bars[max(0,idx-119):idx+1]; m15=[x for x in data["M15"] if int(x["time"])<=t-300][-120:]; h1=[x for x in data["H1"] if int(x["time"])<=t-300][-120:]
        atr5=atr(m5)[-1] if m5 else 0; atr_values.append(atr5); stop_values.append(float(row["stop_distance"]))
        htrend,mtrend=trend(h1),trend(m15)
        dt=datetime.fromtimestamp(t-300,timezone.utc); hour=dt.hour
        enriched.append({**row,"setup_type":"MICRO_SCALP_SWEEP_RECLAIM","bar_index":idx,"atr5":atr5,"h1_context":htrend,"m15_context":mtrend,"context_alignment":"ALIGNED" if htrend==mtrend and htrend!="RANGING" else "CONFLICTING" if htrend!=mtrend and htrend!="RANGING" and mtrend!="RANGING" else "NEUTRAL","session_bucket":session(t-300),"month":dt.strftime("%Y-%m")})
    atr_sorted=sorted(atr_values); stop_sorted=sorted(stop_values)
    for x in enriched:
        x["atr_bucket"]="LOW" if x["atr5"]<=q(atr_sorted,1/3) else "MEDIUM" if x["atr5"]<=q(atr_sorted,2/3) else "HIGH"
        x["spread_pct_stop"]=100*float(x["spread"])/float(x["stop_distance"])
        x["spread_stop_bucket"]="<5%" if float(x["spread_pct_stop"])<5 else "5-10%" if float(x["spread_pct_stop"])<10 else "10-15%" if float(x["spread_pct_stop"])<15 else ">15%"
        sd=float(x["stop_distance"]); x["stop_quartile"]="Q1" if sd<=q(stop_sorted,.25) else "Q2" if sd<=q(stop_sorted,.50) else "Q3" if sd<=q(stop_sorted,.75) else "Q4"
    all_by_target={}; trade_rows=[]
    for target in TARGETS:
        out=[]
        for x in enriched:
            r=outcome(x,bars,int(x["bar_index"]),target); z={**x,"target_r":target,**r}; out.append(z); trade_rows.append(z)
        all_by_target[str(target)]=out
    base=all_by_target["1.25"]
    # Longest losing streak and underwater period from chronological base results.
    base_sorted=sorted(base,key=lambda x:x["timestamp"]); eq=peak=0; underwater=0; longest_under=0; streak=longest_streak=0; curve=[]
    for x in base_sorted:
        r=x["r"] or 0; eq+=r; peak=max(peak,eq); underwater=underwater+1 if eq<peak else 0; longest_under=max(longest_under,underwater); streak=streak+1 if x["outcome"]=="LOSS" else 0; longest_streak=max(longest_streak,streak); curve.append({"timestamp":x["timestamp"],"setup_id":x["setup_id"],"cumulative_r":eq,"r":r,"outcome":x["outcome"]})
    core=summarize(base); core.update({"longest_losing_streak":longest_streak,"longest_underwater_setups":longest_under})
    excursion={"pct_reach_0.5R":pct(sum(x["mfe_r"]>=.5 for x in base),len(base)),"pct_reach_1R":pct(sum(x["mfe_r"]>=1 for x in base),len(base)),"pct_reach_1.25R":pct(sum(x["mfe_r"]>=1.25 for x in base),len(base)),"pct_reach_1.5R":pct(sum(x["mfe_r"]>=1.5 for x in base),len(base)),"pct_reach_2R":pct(sum(x["mfe_r"]>=2 for x in base),len(base)),"mae_r":{"minimum":min(x["mae_r"] for x in base),"p10":q([x["mae_r"] for x in base],.1),"median":statistics.median([x["mae_r"] for x in base]),"p90":q([x["mae_r"] for x in base],.9),"maximum":max(x["mae_r"] for x in base)},"mfe_r":{"minimum":min(x["mfe_r"] for x in base),"p10":q([x["mfe_r"] for x in base],.1),"median":statistics.median([x["mfe_r"] for x in base]),"p90":q([x["mfe_r"] for x in base],.9),"maximum":max(x["mfe_r"] for x in base)}}
    target_summary={str(t):summarize(all_by_target[str(t)]) for t in TARGETS}
    months={m:summarize([x for x in base if x["month"]==m]) for m in sorted(set(x["month"] for x in base))}
    out={"canonical_setup_count":len(canonical),"edge_test_rules":{"independent_each_setup":True,"account_sizing_ignored":True,"paper_state_gates_ignored":True,"max_duration_minutes":30,"same_candle_sl_priority":True},"core_1.25R":core,"target_sensitivity":target_summary,"session":segment_report(base,"session_bucket"),"direction":segment_report(base,"direction"),"setup_type":segment_report(base,"setup_type"),"h1_context":segment_report(base,"h1_context"),"m15_context":segment_report(base,"m15_context"),"context_alignment":segment_report(base,"context_alignment"),"atr_bucket":segment_report(base,"atr_bucket"),"spread_stop_bucket":segment_report(base,"spread_stop_bucket"),"stop_quartile":segment_report(base,"stop_quartile"),"monthly":months,"equity_curve_r":curve,"excursion_analysis":excursion}
    Path(ROOT/"micro_scalp_edge_results.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    fields=["setup_id","timestamp","direction","setup_type","session_bucket","h1_context","m15_context","context_alignment","atr_bucket","spread_stop_bucket","stop_quartile","target_r","entry","stop_loss","stop_distance","take_profit","spread","spread_pct_stop","atr5","outcome","r","mae_r","mfe_r","duration_minutes","exit_price","exit_time"]
    with open(ROOT/"micro_scalp_edge_trades.csv","w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows({k:x.get(k) for k in fields} for x in trade_rows)
    md=["# MICRO_SCALP edge test","","READ-ONLY / PAPER-ONLY. BASELINE and MICRO_SCALP logic were not changed. Account sizing, minimum lot, daily loss, cooldown, open-position, and session-trade gates were excluded from this edge test.","","## Core result at 1.25R",json.dumps(core,indent=2),"","## Edge interpretation","This test assigns an independent historical outcome to every canonical setup. It is not a deployable-account simulation and does not claim live profitability.","","## Target sensitivity",json.dumps(target_summary,indent=2),"","## Session / context / volatility / spread / stop segments",json.dumps({k:out[k] for k in ['session','direction','setup_type','h1_context','m15_context','context_alignment','atr_bucket','spread_stop_bucket','stop_quartile']},indent=2),"","## Monthly stability",json.dumps(months,indent=2),"","## MAE / MFE",json.dumps(excursion,indent=2),"","## Equity / R curve",json.dumps({"ending_cumulative_r":curve[-1]["cumulative_r"],"max_drawdown_r":core["max_drawdown_r"],"longest_underwater_setups":core["longest_underwater_setups"]},indent=2)]
    (ROOT/"micro_scalp_edge_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"canonical":len(canonical),"core":core,"target_sensitivity":target_summary,"monthly":months,"excursion":excursion},indent=2))
if __name__=="__main__": main()
