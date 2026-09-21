from __future__ import annotations
import csv, json, os, statistics, subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import bisect

from micro_scalp import MicroScalpConfig, MicroScalpStrategy
from paper_engine import PaperEngine, StrategyConfig, size_position

ROOT=Path(__file__).resolve().parent
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY=r"C:\Python39\python.exe"

def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX)
    p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch.py")],env=env,text=True,capture_output=True,check=True)
    return json.loads(p.stdout.splitlines()[-1])
def iso(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def session(t):
    h=datetime.fromtimestamp(t,timezone.utc).hour
    return "ASIA" if h<8 else "LONDON" if h<13 else "NEW_YORK" if h<21 else "OFF_HOURS"
def pct(n,d): return 100*n/d if d else 0.0
def q(xs,p):
    if not xs:return None
    return statistics.quantiles(xs,n=100,method="inclusive")[max(0,min(98,int(p*100)-1))] if len(xs)>1 else xs[0]

def run(data,start,end,target_r,balance,risk_pct,label):
    cfg=MicroScalpConfig(target_r=target_r)
    strategy=MicroScalpStrategy(cfg)
    paper_cfg=StrategyConfig(symbol="XAUUSDm",engine_enabled=True,risk_pct=risk_pct,max_scalp_minutes=30)
    paper=PaperEngine(paper_cfg,os.devnull)
    m5=[x for x in data["M5"] if start<=x["time"]<end]
    m15_all=data["M15"]; m15_times=[x["time"] for x in m15_all]
    c=data["contract"]; valid=[]; events=[]; reject=defaultdict(int)
    for i,candle in enumerate(m5):
        t=candle["time"]; paper.process_candle(candle,t+300)
        j=bisect.bisect_right(m15_times,t); m15=m15_all[max(0,j-120):j]
        spread=candle["spread"]*c["point"]; mid=candle["close"]; quote={"bid":mid-spread/2,"ask":mid+spread/2}
        signal=strategy.evaluate(m15,m5[max(0,i-119):i+1],quote,c,iso(t+300))
        if signal.direction=="NONE": reject[signal.invalidation_reason or "no_trade"]+=1; continue
        dist=abs(float(signal.entry)-float(signal.stop_loss)); minlot_risk=(dist/c["tick_size"])*c["tick_value"]*c["min_lot"]
        valid.append({"time":t,"direction":signal.direction,"setup_type":signal.setup_type,"entry":signal.entry,"stop_loss":signal.stop_loss,"take_profit":signal.take_profit,"stop_distance":dist,"min_lot_risk_dollars":minlot_risk,"spread_price":spread,"spread_pct_stop":100*spread/dist,"session":session(t)})
        result=paper.evaluate(signal,balance,c,quote,now=t+300)
        if result["decision"]=="PAPER_ENTRY": events.append({"time":t,"session":session(t),"direction":signal.direction,"setup_type":signal.setup_type,"entry":signal.entry,"stop_loss":signal.stop_loss,"take_profit":signal.take_profit,"lot":result["calculated_lot"],"risk_dollars":result["risk_dollars"],"fingerprint":signal.fingerprint})
    for p in list(paper.positions):
        p.exit=m5[-1]["close"] if m5 else p.entry; p.exit_reason="END_OF_PERIOD"; p.closed_at=iso(end)
        long=p.signal["direction"]=="LONG"; p.realized_r=(p.exit-p.entry)/abs(p.entry-p.stop_loss) if long else (p.entry-p.exit)/abs(p.entry-p.stop_loss); p.status="CLOSED"; paper.positions.remove(p); paper.closed.append(p)
    closed=[]
    for p in paper.closed:
        opened=datetime.fromisoformat(p.opened_at).timestamp(); exited=datetime.fromisoformat(p.closed_at).timestamp() if p.closed_at else end
        closed.append({"time":opened,"exit_time":exited,"direction":p.signal["direction"],"setup_type":p.signal["setup_type"],"entry":p.entry,"stop_loss":p.stop_loss,"take_profit":p.take_profit,"exit":p.exit,"exit_reason":p.exit_reason,"realized_r":p.realized_r,"risk_dollars":p.risk_dollars,"duration_minutes":(exited-opened)/60,"session":session(int(opened))})
    rs=[x["realized_r"] for x in closed]; wins=[x for x in rs if x>0]; losses=[x for x in rs if x<0]; eq=peak=dd=0; streak=longest=0
    for r in rs:
        eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    by_session={}
    for s in sorted(set(x["session"] for x in valid)):
        z=[x for x in closed if x["session"]==s]; by_session[s]={"valid_setups":sum(x["session"]==s for x in valid),"trades":len(z),"wins":sum(x["realized_r"]>0 for x in z),"losses":sum(x["realized_r"]<0 for x in z)}
    days=max((end-start)/86400,1e-9)
    return {"label":label,"start":iso(start),"end":iso(end),"target_r":target_r,"risk_pct":risk_pct,"balance":balance,"valid_setups":len(valid),"executable_setups":len(events),"trades":len(rs),"wins":len(wins),"losses":len(losses),"win_rate_pct":pct(len(wins),len(rs)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"average_r":sum(rs)/len(rs) if rs else 0,"max_drawdown_r":dd,"average_duration_minutes":sum(x["duration_minutes"] for x in closed)/len(closed) if closed else 0,"longest_losing_streak":longest,"trades_per_day":len(rs)/days,"median_stop_distance":statistics.median([x["stop_distance"] for x in valid]) if valid else None,"median_min_lot_risk_dollars":statistics.median([x["min_lot_risk_dollars"] for x in valid]) if valid else None,"p90_min_lot_risk_dollars":q([x["min_lot_risk_dollars"] for x in valid],.9),"median_spread_pct_stop":statistics.median([x["spread_pct_stop"] for x in valid]) if valid else None,"p90_spread_pct_stop":q([x["spread_pct_stop"] for x in valid],.9),"frequency_by_session":by_session,"rejections":dict(reject),"trades_detail":closed,"valid_setup_detail":valid,"events":events}

def main():
    data=fetch(); bars=data["M5"]; end=max(x["time"] for x in bars[:-1]); day=86400; equity=float(data["account"].get("equity") or 100.0)
    periods=[("30_days",end-42*day,end),("90_days",end-126*day,end),("6_months",end-184*day,end)]
    results={"source":"local MT5 Exness-MT5Real27 XAUUSDm history","contract":data["contract"],"account":data["account"],"methodology":{"execution":"M5 completed candles","context":"M5/M15","holding_target_minutes":"5-30","same_candle_priority":"SL first","baseline_unchanged":True},"periods":{},"target_sensitivity":{},"balance_sensitivity":{}}
    for label,s,e in periods:
        results["periods"][label]={"target_1.25R":run(data,s,e,1.25,equity,.5,label+"_1.25R")}
        results["target_sensitivity"][label]={str(rr):run(data,s,e,rr,equity,.5,label+f"_{rr}R") for rr in [1.0,1.25,1.5]}
        results["balance_sensitivity"][label]={str(rp):run(data,s,e,1.25,equity,rp,label+f"_{rp}pct") for rp in [.5,1.0,2.0]}
    six=results["periods"]["6_months"]["target_1.25R"]; risks=[x["min_lot_risk_dollars"] for x in six["valid_setup_detail"]]
    results["practical_minimum_balance"]={str(rp):(q(risks,.9)/(rp/100.0) if risks else None) for rp in [.5,1.0,2.0]}
    baseline_path=ROOT/"backtest_results.json"
    if baseline_path.exists():
        baseline=json.loads(baseline_path.read_text())
        results["baseline_comparison"]={k:{m:baseline["periods"].get(k,{}).get(m) for m in ["trades","valid_setups","win_rate_pct","profit_factor","expectancy_r","average_r","max_drawdown_r","trades_per_day","pct_setups_skipped_minimum_lot"]} for k in ["30_trading_days","90_trading_days","6_months"]}
    results["decision"]="A" if results["balance_sensitivity"]["6_months"]["0.5"]["executable_setups"]>0 and results["balance_sensitivity"]["6_months"]["0.5"]["expectancy_r"]>0 else "B" if results["balance_sensitivity"]["6_months"]["0.5"]["executable_setups"]>0 else "C"
    Path(ROOT/"micro_scalp_results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    with open(ROOT/"micro_scalp_trades.csv","w",newline="",encoding="utf-8") as f:
        fields=["period","risk_pct","target_r","time","exit_time","direction","setup_type","entry","stop_loss","take_profit","exit","exit_reason","realized_r","risk_dollars","duration_minutes","session"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for label,block in results["balance_sensitivity"].items():
            for risk_label,x in block.items():
                for t in x["trades_detail"]: w.writerow({"period":label,"risk_pct":x["risk_pct"],"target_r":x["target_r"],**t})
    md=["# XAUUSDm MICRO_SCALP validation","","READ-ONLY / PAPER-ONLY. BASELINE was not replaced or widened. No live order submission was used.","","## Design","","Separate M5 completed-candle sweep/reclaim setup with M5/M15 context. Stops are below/above the sweep extreme plus the larger of 0.10 ATR, 1.5x spread, and broker stop-level minimum. Entries are rejected when stop is too large versus ATR, spread exceeds 35% of stop, or opposing structure cannot support the selected R target. Paper exits use 30-minute maximum hold and conservative SL-first same-candle handling.","","## 1.25R target results at current account equity"]
    for label,x in results["periods"].items():
        p=x["target_1.25R"]; md += [f"### {label}",f"- Valid setups {p['valid_setups']}; executable at 0.5% {p['executable_setups']}; median stop {p['median_stop_distance']}; median 0.01-lot risk ${p['median_min_lot_risk_dollars']:.2f}; P90 ${p['p90_min_lot_risk_dollars']:.2f}",f"- Trades {p['trades']} ({p['wins']}W/{p['losses']}L); win rate {p['win_rate_pct']:.1f}%; PF {p['profit_factor']}; expectancy {p['expectancy_r']:.3f}R; max DD {p['max_drawdown_r']:.3f}R",f"- Avg duration {p['average_duration_minutes']:.1f} min; trades/day {p['trades_per_day']:.3f}; median spread/stop {p['median_spread_pct_stop']:.1f}%",f"- Sessions {json.dumps(p['frequency_by_session'])}",""]
    md += ["## Executability by risk percentage",json.dumps({k:{rp:{m:v[m] for m in ['valid_setups','executable_setups','trades','win_rate_pct','profit_factor','expectancy_r','max_drawdown_r','trades_per_day']} for rp,v in block.items()} for k,block in results['balance_sensitivity'].items()},indent=2),"","## Target sensitivity",json.dumps({k:{rr:{m:v[m] for m in ['valid_setups','executable_setups','trades','win_rate_pct','profit_factor','expectancy_r','max_drawdown_r']} for rr,v in block.items()} for k,block in results['target_sensitivity'].items()},indent=2),"","## Practical minimum balance",json.dumps(results["practical_minimum_balance"],indent=2),"","## Final decision",f"{results['decision']}. This decision is limited to the available historical sample; it is not a profitability claim."]
    (ROOT/"micro_scalp_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"decision":results["decision"],"periods":{k:{m:v['target_1.25R'][m] for m in ['valid_setups','executable_setups','trades','win_rate_pct','profit_factor','expectancy_r']} for k,v in results['periods'].items()},"minimum_balances":results['practical_minimum_balance']},indent=2))
if __name__=="__main__": main()
