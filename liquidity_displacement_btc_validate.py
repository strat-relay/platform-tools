from __future__ import annotations
import bisect, csv, json, os, statistics, subprocess
from datetime import datetime, timezone
from pathlib import Path
from liquidity_displacement import LiquidityDisplacementStrategy, LiquidityDisplacementConfig
from liquidity_displacement_validate import iso, pct, session, trend

ROOT=Path(__file__).resolve().parent; WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"; PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"; WINPY=r"C:\Python39\python.exe"; TARGET=1.25

def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_btc.py")],env=env,text=True,capture_output=True,timeout=120,check=True); return json.loads(p.stdout.splitlines()[-1])

def outcome(row,bars,fill_i):
    e=float(row["entry"]); sl=float(row["stop_loss"]); risk=abs(e-sl); long=row["direction"]=="LONG"; tp=e+risk*TARGET if long else e-risk*TARGET
    for step in range(25):
        bar=bars[fill_i+step]; hit_sl=float(bar["low"])<=sl if long else float(bar["high"])>=sl; hit_tp=float(bar["high"])>=tp if long else float(bar["low"])<=tp
        if step>=24:
            px=float(bar["close"]); return {"outcome":"TIME_EXIT","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":step*5}
        if hit_sl or hit_tp:
            px=sl if hit_sl else tp; return {"outcome":"LOSS" if hit_sl else "WIN","r":(px-e)/risk if long else (e-px)/risk,"duration_minutes":step*5}
    return {"outcome":"INVALID_AFTER_SIGNAL","r":None,"duration_minutes":120}

def metrics(rows):
    filled=[x for x in rows if x["status"]=="FILLED"]; resolved=[x for x in filled if x.get("r") is not None]; rs=[float(x["r"]) for x in resolved]; wins=[x for x in resolved if x["outcome"]=="WIN"]; losses=[x for x in resolved if x["outcome"]=="LOSS"]; eq=peak=dd=0.; streak=longest=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    gp,gl=sum(float(x["r"]) for x in wins),sum(float(x["r"]) for x in losses); durations=[x["duration_minutes"] for x in resolved]
    return {"setups":len(rows),"filled":len(filled),"fill_rate_pct":pct(len(filled),len(rows)),"unfilled":sum(x["status"]=="UNFILLED" for x in rows),"wins":len(wins),"losses":len(losses),"time_exits":sum(x["outcome"]=="TIME_EXIT" for x in resolved),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":gp/abs(gl) if gl else None,"expectancy_r":sum(rs)/len(rs) if rs else 0.,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest,"average_duration_minutes":statistics.mean(durations) if durations else 0.,"median_duration_minutes":statistics.median(durations) if durations else 0.}

def main():
    data=fetch(); bars=data["M5"]; end=max(int(x["time"]) for x in bars[:-1]); start=end-184*86400; m5=[x for x in bars if start<=int(x["time"])<end]; m15=data["M15"]; mt=[int(x["time"]) for x in m15]; c=data["contract"]; strategy=LiquidityDisplacementStrategy(LiquidityDisplacementConfig(symbol="BTCUSDm",target_r=TARGET,max_hold_minutes=120,max_retrace_candles=3,max_structure_break_candles=5,min_body_atr=.5,min_body_median_multiple=1.,min_close_location=.6,atr_buffer_fraction=.1)); setups=[]
    for i in range(40,len(m5)-25):
        t=int(m5[i]["time"]); j=bisect.bisect_right(mt,t); ctx=m15[max(0,j-120):j]; spread=float(m5[i]["spread"])*c["point"]; q={"bid":float(m5[i]["close"])-spread/2,"ask":float(m5[i]["close"])+spread/2}; cand=strategy.evaluate(ctx,m5,q,c,iso(t+300),i)
        if not cand: continue
        fi=cand["fill_index"]; ei=fi if cand["status"]=="FILLED" else cand["displacement_index"]; et=int(m5[ei]["time"])+300; dt=datetime.fromtimestamp(et,timezone.utc); risk=float(cand["risk"]); h1=[x for x in data["H1"] if int(x["time"])<=t][-120:]
        setups.append({"setup_id":f"BTCV1-{t}-{i}-{cand['direction']}","timestamp":iso(et),"month":dt.strftime("%Y-%m"),"session":session(et),"direction":cand["direction"],"setup_type":cand["setup_type"],"entry_type":cand["entry_type"],"entry":cand["entry"],"stop_loss":cand["stop_loss"],"stop_distance":risk,"spread":cand["spread"],"spread_stop_ratio":cand["spread"]/risk if risk else 0,"atr":cand["atr"],"h1_trend":trend(h1),"m15_trend":trend(ctx),"status":cand["status"],"fill_index":fi,"min_lot_risk_dollars":(risk/c["tick_size"])*c["tick_value"]*c["min_lot"]})
    rows=[]
    for x in setups:
        y=dict(x); y.update(outcome(x,m5,int(x["fill_index"])) if x["status"]=="FILLED" else {"outcome":"UNFILLED","r":None,"duration_minutes":0}); rows.append(y)
    months=sorted({x["month"] for x in rows}); monthly={m:metrics([x for x in rows if x["month"]==m]) for m in months}; dist=[float(x["min_lot_risk_dollars"]) for x in setups]; result={"strategy":"LIQUIDITY_DISPLACEMENT_SCALP_V1","symbol":"BTCUSDm","rules":{"target_r":TARGET,"max_retrace_candles":3,"max_hold_minutes":120,"m1_used":False},"source_sha256":"4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea","period":{"start":iso(start),"end":iso(end)},"contract":c,"setups_count":len(setups),"metrics":metrics(rows),"monthly":monthly,"risk_at_001":{"minimum":min(dist) if dist else 0,"median":statistics.median(dist) if dist else 0,"p90":statistics.quantiles(dist,n=10,method="inclusive")[8] if len(dist)>1 else (dist[0] if dist else 0)}}
    (ROOT/"liquidity_displacement_btc_results.json").write_text(json.dumps(result,indent=2)); fields=list(rows[0].keys()) if rows else ["setup_id"]
    with (ROOT/"liquidity_displacement_btc_trades.csv").open("w",newline="") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    (ROOT/"liquidity_displacement_btc_summary.md").write_text("\n".join(["# V1 BTCUSDm historical validation","","READ-ONLY / PAPER-ONLY. XAUUSDm V1 was not modified.","",json.dumps(result,indent=2)]))
    print(json.dumps(result,indent=2))
if __name__=="__main__": main()
