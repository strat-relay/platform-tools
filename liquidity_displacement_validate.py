from __future__ import annotations
import csv,json,os,statistics,subprocess,bisect
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from paper_engine import atr,ema
from liquidity_displacement import LiquidityDisplacementStrategy,LiquidityDisplacementConfig

ROOT=Path(__file__).resolve().parent; WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"; PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"; WINPY=r"C:\Python39\python.exe"; TARGETS=[1.,1.25,1.5,2.]
def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch.py")],env=env,text=True,capture_output=True,check=True); return json.loads(p.stdout.splitlines()[-1])
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
def pct(n,d):return 100*n/d if d else 0
def q(xs,p):return statistics.quantiles(xs,n=100,method="inclusive")[max(0,min(98,int(p*100)-1))] if len(xs)>1 else (xs[0] if xs else None)
def session(t):
    h=datetime.fromtimestamp(t,timezone.utc).hour
    return "LONDON_NEW_YORK_OVERLAP" if 13<=h<16 else "ASIA" if h<8 else "LONDON" if h<13 else "NEW_YORK" if h<21 else "OTHER"
def trend(xs):
    if len(xs)<50:return "RANGING"
    c=[float(x["close"]) for x in xs]; f,s=ema(c,20)[-1],ema(c,50)[-1]; a=atr(xs)[-1]
    return "RANGING" if a<=0 or abs(f-s)<=.1*a else "BULLISH" if f>s else "BEARISH"
def outcome(row,bars,fill_i,target):
    e=float(row["entry"]); sl=float(row["stop_loss"]); risk=abs(e-sl); long=row["direction"]=="LONG"; tp=e+risk*target if long else e-risk*target; r=None; exit_reason="TIME_EXIT"; exit_price=None; duration=0
    for step in range(0,25):
        j=fill_i+step
        if j>=len(bars): exit_reason="INVALID_AFTER_SIGNAL"; break
        c=bars[j]; high=float(c["high"]); low=float(c["low"]); duration=step*5
        if step>=24: exit_price=float(c["close"]); exit_reason="TIME_EXIT"; break
        hit_sl=low<=sl if long else high>=sl; hit_tp=high>=tp if long else low<=tp
        if hit_sl or hit_tp: exit_price=sl if hit_sl else tp; exit_reason="LOSS" if hit_sl else "WIN"; break
    if exit_price is None:return {"outcome":exit_reason,"r":None,"duration_minutes":duration,"exit_price":None}
    return {"outcome":exit_reason,"r":(exit_price-e)/risk if long else (e-exit_price)/risk,"duration_minutes":duration,"exit_price":exit_price}
def metrics(rows):
    filled=[x for x in rows if x["status"]=="FILLED"]; rs=[float(x["r"]) for x in filled if x.get("r") is not None]; wins=[x["r"] for x in filled if x["outcome"]=="WIN"]; losses=[x["r"] for x in filled if x["outcome"]=="LOSS"]; eq=peak=dd=0; streak=longest=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    return {"setups":len(rows),"filled":len(filled),"fill_rate_pct":pct(len(filled),len(rows)),"unfilled":sum(x["status"]=="UNFILLED" for x in rows),"resolved_trades":len(rs),"wins":len(wins),"losses":len(losses),"time_exits":sum(x.get("outcome")=="TIME_EXIT" for x in filled),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest,"average_duration_minutes":sum(x["duration_minutes"] for x in filled)/len(filled) if filled else 0,"median_duration_minutes":statistics.median([x["duration_minutes"] for x in filled]) if filled else 0}
def main():
    d=fetch(); bars=d["M5"]; times=[int(x["time"]) for x in bars]; end=max(times[:-1]); start=end-184*86400; m5=[x for x in bars if start<=x["time"]<end]; m15=d["M15"]; m15t=[int(x["time"]) for x in m15]; c=d["contract"]; strategy=LiquidityDisplacementStrategy(LiquidityDisplacementConfig())
    setups=[]
    for i in range(40,len(m5)-25):
        t=int(m5[i]["time"]); j=bisect.bisect_right(m15t,t); context=m15[max(0,j-120):j]; spread=m5[i]["spread"]*c["point"]; quote={"bid":m5[i]["close"]-spread/2,"ask":m5[i]["close"]+spread/2}; candidate=strategy.evaluate(context,m5,quote,c,iso(t+300),i)
        if not candidate:continue
        h1=[x for x in d["H1"] if int(x["time"])<=t][-120:]; m15ctx=context; a=float(candidate["atr"]); risk=float(candidate["risk"]); event_i=candidate["fill_index"] if candidate["status"]=="FILLED" else candidate["displacement_index"]; event_time=int(m5[event_i]["time"])+300; dt=datetime.fromtimestamp(event_time,timezone.utc)
        row={"setup_id":f"LDS-{t}-{i}","timestamp":iso(event_time),"timestamp_epoch":event_time,"month":dt.strftime("%Y-%m"),"session":session(event_time),"direction":candidate["direction"],"setup_type":candidate["setup_type"],"liquidity_type":candidate["liquidity_type"],"level_age_candles":candidate["level_age_candles"],"touch_count":candidate["touch_count"],"sweep_level":candidate["sweep_level"],"sweep_distance":candidate["sweep_distance"],"wick_penetration":candidate["wick_penetration"],"reclaim_strength":abs(float(m5[i]["close"])-float(candidate["sweep_level"]))/a if a else 0,"body_atr":candidate["body_atr"],"body_median_multiple":None,"close_location":candidate["close_location"],"displacement_range":candidate["displacement_range"],"impulse_candles":1,"micro_swing_broken":candidate["break_level"],"break_distance":candidate["break_distance"],"time_sweep_to_break":candidate["time_sweep_to_break"],"time_break_to_entry":candidate["time_break_to_entry"],"retest_depth":.5,"entry_delay_candles":candidate["entry_delay_candles"],"entry_type":candidate["entry_type"],"entry":candidate["entry"],"stop_loss":candidate["stop_loss"],"stop_distance":risk,"raw_structural_stop":candidate["sweep_extreme"],"atr_buffer":abs(candidate["stop_loss"]-candidate["sweep_extreme"]),"take_profit_1.25R":candidate["signal"].take_profit if candidate["status"]=="FILLED" else None,"next_opposing_liquidity":candidate["next_opposing_level"],"spread":candidate["spread"],"spread_stop_ratio":candidate["spread"]/risk,"atr":a,"h1_trend":trend(h1),"m15_trend":trend(m15ctx),"status":candidate["status"],"fill_index":candidate["fill_index"],"min_lot_risk_dollars":(risk/c["tick_size"])*c["tick_value"]*c["min_lot"],"tick_size":c["tick_size"],"tick_value":c["tick_value"],"contract_size":c["contract_size"],"broker_min_lot":c["min_lot"],"broker_lot_step":c["lot_step"]}
        setups.append(row)
    atrs=[x["atr"] for x in setups]; atrs.sort()
    for x in setups:x["atr_percentile"]=pct(sum(a<=float(x["atr"]) for a in atrs),len(atrs))
    by_target={}
    for target in TARGETS:
        rows=[]
        for x in setups:
            y=dict(x); y["target_r"]=target
            if x["status"]=="FILLED": y.update(outcome(x,m5,int(x["fill_index"]),target))
            else: y.update({"outcome":"UNFILLED","r":None,"duration_minutes":0,"exit_price":None})
            rows.append(y)
        by_target[str(target)]=rows
    base=by_target["1.25"]; monthly={m:metrics([x for x in base if x["month"]==m]) for m in sorted(set(x["month"] for x in base))}
    static_balances={}
    for bal in [100,150,250,500,750,1000]:static_balances[str(bal)]={str(rp):{"count":sum(float(x["min_lot_risk_dollars"])<=bal*rp/100 for x in setups),"pct":pct(sum(float(x["min_lot_risk_dollars"])<=bal*rp/100 for x in setups),len(setups))} for rp in [.5,1,2]}
    dist=[float(x["min_lot_risk_dollars"]) for x in setups]
    micro=json.load(open(ROOT/"micro_scalp_edge_results.json")); microaudit=json.load(open(ROOT/"micro_scalp_audit_results.json"))
    months=sorted(set(x["month"] for x in setups)); discovery=months[:4]; validation=months[-2:]
    out={"strategy":"LIQUIDITY_DISPLACEMENT_SCALP","rules":{"sequence":"sweep -> reclaim -> displacement -> micro shift -> 50% displacement retracement","m1_used":False,"max_hold_minutes":120,"max_retrace_candles":3,"no_regime_prefilters":True},"period":{"start":iso(start),"end":iso(end)},"contract":c,"setups_count":len(setups),"unfilled_count":sum(x["status"]=="UNFILLED" for x in setups),"target_results":{k:metrics(v) for k,v in by_target.items()},"monthly_1.25R":monthly,"discovery_validation":{"discovery_months":discovery,"validation_months":validation,"discovery":metrics([x for x in base if x["month"] in discovery]),"validation":metrics([x for x in base if x["month"] in validation])},"small_account_feasibility":{"curve":static_balances,"risk_distribution":{"minimum":min(dist),"p10":q(dist,.1),"median":statistics.median(dist),"p90":q(dist,.9),"maximum":max(dist)}},"comparison":{"MICRO_SCALP":{"setups":micro["canonical_setup_count"],"expectancy_r":micro["core_1.25R"]["expectancy_r"],"profit_factor":micro["core_1.25R"]["profit_factor"],"max_drawdown_r":micro["core_1.25R"]["max_drawdown_r"],"longest_losing_streak":micro["core_1.25R"]["longest_losing_streak"],"median_duration_minutes":micro["core_1.25R"]["median_duration_minutes"],"median_stop_distance":statistics.median([float(x["stop_distance"]) for x in csv.DictReader(open(ROOT/"micro_scalp_canonical_setups.csv"))]),"median_min_lot_risk_dollars":microaudit["required_balance_distributions"]["1"]["median_balance"]*.01,"pct_executable_150_1pct":microaudit["static_executability"]["account_size_curve"][1]["1"]["pct"]},"LIQUIDITY_DISPLACEMENT_SCALP":{}}
    }
    out["comparison"]["LIQUIDITY_DISPLACEMENT_SCALP"]={"setups":len(setups),"expectancy_r":out["target_results"]["1.25"]["expectancy_r"],"profit_factor":out["target_results"]["1.25"]["profit_factor"],"max_drawdown_r":out["target_results"]["1.25"]["max_drawdown_r"],"longest_losing_streak":out["target_results"]["1.25"]["longest_losing_streak"],"median_duration_minutes":out["target_results"]["1.25"]["median_duration_minutes"],"median_stop_distance":statistics.median([float(x["stop_distance"]) for x in setups]),"median_min_lot_risk_dollars":statistics.median(dist),"pct_executable_150_1pct":static_balances["150"]["1"]["pct"]}
    out["decision"]="A" if out["discovery_validation"]["validation"]["expectancy_r"]>=.1 and out["target_results"]["1.25"]["profit_factor"]>=1.2 else "B" if out["discovery_validation"]["validation"]["expectancy_r"]>0 else "D"
    Path(ROOT/"liquidity_displacement_results.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    fields=list(base[0].keys())
    with open(ROOT/"liquidity_displacement_trades.csv","w",newline="",encoding="utf-8") as f:w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(base)
    md=["# LIQUIDITY_DISPLACEMENT_SCALP research","","READ-ONLY / PAPER-ONLY. BASELINE and MICRO_SCALP were not modified. M1 was not used; M5 setup detection and M15 context were used.","","## Fixed rule sequence","sweep -> reclaim -> displacement -> micro-structure break -> 50% displacement retracement, with a three-candle retracement window and 120-minute maximum hold.","","## Target results",json.dumps(out["target_results"],indent=2),"","## Discovery / validation",json.dumps(out["discovery_validation"],indent=2),"","## Monthly 1.25R",json.dumps(monthly,indent=2),"","## Small-account feasibility",json.dumps(out["small_account_feasibility"],indent=2),"","## BASELINE comparison",json.dumps(out["comparison"],indent=2),"","## Decision",f"{out['decision']}. Rules were fixed before replay; no target or regime filter was selected using validation data."]
    (ROOT/"liquidity_displacement_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"decision":out["decision"],"setups":len(setups),"unfilled":out["unfilled_count"],"targets":out["target_results"],"discovery_validation":out["discovery_validation"]},indent=2))
if __name__=="__main__":main()
