from __future__ import annotations
import bisect, csv, json, os, statistics, subprocess
from datetime import datetime, timezone
from pathlib import Path
from liquidity_displacement import LiquidityDisplacementStrategy, LiquidityDisplacementConfig
from liquidity_displacement_btc_validate import outcome, metrics
from liquidity_displacement_validate import iso, pct, session, trend

ROOT=Path(__file__).resolve().parent; WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"; PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"; WINPY=r"C:\Python39\python.exe"; SYMBOLS=["EURUSDm","GBPUSDm","USDJPYm","USTEC_x100m","USTECm"]; RETRACE_CANDLES=int(os.environ.get("RETRACE_CANDLES","3")); SUFFIX=f"_{RETRACE_CANDLES}c"

def fetch(symbol):
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_symbol.py"),symbol],env=env,text=True,capture_output=True,timeout=120,check=True); return json.loads(p.stdout.splitlines()[-1])

def validate(symbol):
    data=fetch(symbol); bars=data["M5"]
    if len(bars)<100: return {"symbol":symbol,"error":"insufficient M5 history","bars":len(bars)}
    end=max(int(x["time"]) for x in bars[:-1]); start=end-184*86400; m5=[x for x in bars if start<=int(x["time"])<end]; m15=data["M15"]; mt=[int(x["time"]) for x in m15]; c=data["contract"]
    strategy=LiquidityDisplacementStrategy(LiquidityDisplacementConfig(symbol=symbol,target_r=1.25,max_hold_minutes=120,max_retrace_candles=RETRACE_CANDLES,max_structure_break_candles=5,min_body_atr=.5,min_body_median_multiple=1.,min_close_location=.6,atr_buffer_fraction=.1)); rows=[]
    for i in range(40,len(m5)-25):
        t=int(m5[i]["time"]); j=bisect.bisect_right(mt,t); ctx=m15[max(0,j-120):j]; spread=float(m5[i]["spread"])*c["point"]; q={"bid":float(m5[i]["close"])-spread/2,"ask":float(m5[i]["close"])+spread/2}; cand=strategy.evaluate(ctx,m5,q,c,iso(t+300),i)
        if not cand: continue
        fi=cand["fill_index"]; ei=fi if cand["status"]=="FILLED" else cand["displacement_index"]; et=int(m5[ei]["time"])+300; dt=datetime.fromtimestamp(et,timezone.utc); risk=float(cand["risk"]); h1=[x for x in data["H1"] if int(x["time"])<=t][-120:]
        row={"setup_id":f"{symbol}-V1-{t}-{i}-{cand['direction']}","timestamp":iso(et),"month":dt.strftime("%Y-%m"),"session":session(et),"direction":cand["direction"],"setup_type":cand["setup_type"],"entry_type":cand["entry_type"],"entry":cand["entry"],"stop_loss":cand["stop_loss"],"stop_distance":risk,"spread":cand["spread"],"spread_stop_ratio":cand["spread"]/risk if risk else 0,"atr":cand["atr"],"h1_trend":trend(h1),"m15_trend":trend(ctx),"status":cand["status"],"fill_index":fi,"min_lot_risk_dollars":(risk/c["tick_size"])*c["tick_value"]*c["min_lot"]}
        row.update(outcome(row,m5,int(fi)) if cand["status"]=="FILLED" else {"outcome":"UNFILLED","r":None,"duration_minutes":0}); rows.append(row)
    months=sorted({x["month"] for x in rows}); dist=[float(x["min_lot_risk_dollars"]) for x in rows]; filled=[x for x in rows if x["status"]=="FILLED"]
    result={"symbol":symbol,"strategy":"LIQUIDITY_DISPLACEMENT_SCALP_V1","max_retrace_candles":RETRACE_CANDLES,"source_sha256":"4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea","period":{"start":iso(start),"end":iso(end)},"contract":c,"metrics":metrics(rows),"monthly":{m:metrics([x for x in rows if x["month"]==m]) for m in months},"risk_at_001":{"minimum":min(dist) if dist else 0,"median":statistics.median(dist) if dist else 0,"p90":statistics.quantiles(dist,n=10,method="inclusive")[8] if len(dist)>1 else (dist[0] if dist else 0)}}
    fields=list(rows[0].keys()) if rows else ["setup_id"]; path=ROOT/f"liquidity_displacement_{symbol}_v1{SUFFIX}_trades.csv"
    with path.open("w",newline="") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    return result

def main():
    results={}
    for symbol in SYMBOLS:
        try: results[symbol]=validate(symbol)
        except Exception as exc: results[symbol]={"symbol":symbol,"error":str(exc)}
        print(json.dumps({"symbol":symbol,"metrics":results[symbol].get("metrics"),"error":results[symbol].get("error")},indent=2),flush=True)
    (ROOT/f"liquidity_displacement_multi_results{SUFFIX}.json").write_text(json.dumps(results,indent=2))
    lines=[f"# V1 multi-instrument historical validation ({RETRACE_CANDLES} candle expiry)","","READ-ONLY / PAPER-ONLY. XAUUSDm V1 and its runners were not modified.","", "| Symbol | Setups | Fills | Fill rate | Win rate | PF | Expectancy R | Cum R | Max DD | Median duration |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s,r in results.items():
        m=r.get("metrics",{}); lines.append(f"| {s} | {m.get('setups','—')} | {m.get('filled','—')} | {m.get('fill_rate_pct',0):.1f}% | {m.get('win_rate_pct',0):.1f}% | {m.get('profit_factor','—')} | {m.get('expectancy_r',0):.3f} | {m.get('cumulative_r',0):.2f} | {m.get('max_drawdown_r',0):.2f} | {m.get('median_duration_minutes',0):.1f}m |" if m else f"| {s} | error | error | error | error | error | error | error | error | error |")
    (ROOT/f"liquidity_displacement_multi_summary{SUFFIX}.md").write_text("\n".join(lines))
if __name__=="__main__": main()
