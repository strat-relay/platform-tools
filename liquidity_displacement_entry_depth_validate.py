from __future__ import annotations
import bisect, csv, json, os, statistics, subprocess
from datetime import datetime, timezone
from pathlib import Path
from liquidity_displacement import LiquidityDisplacementStrategy, LiquidityDisplacementConfig
from liquidity_displacement_validate import iso, pct, session, trend

ROOT=Path(__file__).resolve().parent; WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"; PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"; WINPY=r"C:\Python39\python.exe"; SYMBOLS=["XAUUSDm","USDJPYm","EURUSDm"]; DEPTHS={"25":.25,"33":1/3,"50":.5}; TARGET=1.25

def fetch(symbol):
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_symbol.py"),symbol],env=env,text=True,capture_output=True,timeout=120,check=True); return json.loads(p.stdout.splitlines()[-1])

def settle(row,bars,fill_i):
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
    gp,gl=sum(float(x["r"]) for x in wins),sum(float(x["r"]) for x in losses); dur=[x["duration_minutes"] for x in resolved]
    return {"setups":len(rows),"filled":len(filled),"fill_rate_pct":pct(len(filled),len(rows)),"wins":len(wins),"losses":len(losses),"time_exits":sum(x["outcome"]=="TIME_EXIT" for x in resolved),"win_rate_pct":pct(len(wins),len(wins)+len(losses)),"profit_factor":gp/abs(gl) if gl else None,"expectancy_r":sum(rs)/len(rs) if rs else 0.,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest,"average_duration_minutes":statistics.mean(dur) if dur else 0.,"median_duration_minutes":statistics.median(dur) if dur else 0.}

def run_symbol(symbol):
    data=fetch(symbol); bars=data["M5"]; end=max(int(x["time"]) for x in bars[:-1]); start=end-184*86400; m5=[x for x in bars if start<=int(x["time"])<end]; m15=data["M15"]; mt=[int(x["time"]) for x in m15]; c=data["contract"]; strategy=LiquidityDisplacementStrategy(LiquidityDisplacementConfig(symbol=symbol,target_r=TARGET,max_hold_minutes=120,max_retrace_candles=5,max_structure_break_candles=5,min_body_atr=.5,min_body_median_multiple=1.,min_close_location=.6,atr_buffer_fraction=.1)); base_candidates=[]
    for i in range(40,len(m5)-25):
        t=int(m5[i]["time"]); j=bisect.bisect_right(mt,t); ctx=m15[max(0,j-120):j]; spread=float(m5[i]["spread"])*c["point"]; q={"bid":float(m5[i]["close"])-spread/2,"ask":float(m5[i]["close"])+spread/2}; cand=strategy.find_candidate(ctx,m5,i,q,c,iso(t+300))
        if cand: base_candidates.append((i,cand,ctx))
    depth_rows={}
    for label,fraction in DEPTHS.items():
        rows=[]
        for i,cand,ctx in base_candidates:
            d=m5[cand["displacement_index"]]; dlow,dhigh=float(d["low"]),float(d["high"]); entry=dhigh-(dhigh-dlow)*fraction if cand["direction"]=="LONG" else dlow+(dhigh-dlow)*fraction; stop=float(cand["stop_loss"]); row={"setup_id":f"{symbol}-DEPTH{label}-{m5[i]['time']}-{i}-{cand['direction']}","timestamp":iso(int(m5[i]["time"])+300),"month":datetime.fromtimestamp(int(m5[i]["time"]),timezone.utc).strftime("%Y-%m"),"session":session(int(m5[i]["time"])+300),"direction":cand["direction"],"entry_depth":label,"entry":entry,"stop_loss":stop,"stop_distance":abs(entry-stop),"spread":cand["spread"],"spread_stop_ratio":cand["spread"]/abs(entry-stop) if abs(entry-stop) else 0,"atr":cand["atr"],"setup_type":cand["setup_type"],"status":"UNFILLED","fill_index":None,"h1_trend":trend([x for x in data["H1"] if int(x["time"])<=int(m5[i]["time"])][-120:]),"m15_trend":trend(ctx)}
            for fill_i in range(cand["displacement_index"]+1,min(len(m5),cand["displacement_index"]+1+5)):
                bar=m5[fill_i]; touched=float(bar["low"])<=entry if cand["direction"]=="LONG" else float(bar["high"])>=entry; held=float(bar["close"])>=entry if cand["direction"]=="LONG" else float(bar["close"])<=entry
                if touched and held: row["status"]="FILLED"; row["fill_index"]=fill_i; break
            row.update(settle(row,m5,row["fill_index"]) if row["status"]=="FILLED" else {"outcome":"UNFILLED","r":None,"duration_minutes":0}); rows.append(row)
        depth_rows[label]=rows
    base_by_id={x["setup_id"].replace("DEPTH50","DEPTH"):x for x in depth_rows["50"]}
    incremental={}
    for label in ("25","33"):
        added=[]
        for row in depth_rows[label]:
            key=row["setup_id"].replace(f"DEPTH{label}","DEPTH")
            if row["status"]=="FILLED" and base_by_id.get(key,{}).get("status")!="FILLED": added.append(row)
        incremental[label]={"trades_not_filled_at_50":len(added),"metrics":metrics(added)}
    result={"symbol":symbol,"strategy":"LIQUIDITY_DISPLACEMENT_SCALP_V1_ENTRY_DEPTH","source_sha256":"4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea","period":{"start":iso(start),"end":iso(end)},"rules":{"max_retrace_candles":5,"target_r":TARGET,"entry_depths":{"25":"25% from displacement extreme","33":"33.33% from displacement extreme","50":"50% control"}},"by_depth":{k:metrics(v) for k,v in depth_rows.items()},"incremental_vs_50":incremental}
    for label,rows in depth_rows.items():
        fields=list(rows[0].keys()) if rows else ["setup_id"]
        with (ROOT/f"liquidity_displacement_{symbol}_entry_{label}pct_trades.csv").open("w",newline="") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    return result

def main():
    results={}
    for symbol in SYMBOLS:
        try: results[symbol]=run_symbol(symbol)
        except Exception as exc: results[symbol]={"symbol":symbol,"error":str(exc)}
        print(json.dumps({"symbol":symbol,"by_depth":results[symbol].get("by_depth"),"incremental_vs_50":results[symbol].get("incremental_vs_50"),"error":results[symbol].get("error")},indent=2),flush=True)
    (ROOT/"liquidity_displacement_entry_depth_results.json").write_text(json.dumps(results,indent=2)); lines=["# V1 entry-depth historical validation","","READ-ONLY / PAPER-ONLY. Same V1 setup logic; 5-candle expiry; target recalculated to 1.25R from each entry.","","| Symbol | Depth | Fills | Fill rate | Win rate | PF | Expectancy R | Cum R | Max DD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s,r in results.items():
        for d,m in r.get("by_depth",{}).items(): lines.append(f"| {s} | {d}% | {m['filled']} | {m['fill_rate_pct']:.1f}% | {m['win_rate_pct']:.1f}% | {m['profit_factor']} | {m['expectancy_r']:.3f} | {m['cumulative_r']:.2f} | {m['max_drawdown_r']:.2f} |")
        for d,x in r.get("incremental_vs_50",{}).items(): lines.append(f"| {s} | {d}% incremental vs 50% | {x['metrics']['filled']} | — | {x['metrics']['win_rate_pct']:.1f}% | {x['metrics']['profit_factor']} | {x['metrics']['expectancy_r']:.3f} | {x['metrics']['cumulative_r']:.2f} | {x['metrics']['max_drawdown_r']:.2f} |")
    (ROOT/"liquidity_displacement_entry_depth_summary.md").write_text("\n".join(lines))
if __name__=="__main__": main()
