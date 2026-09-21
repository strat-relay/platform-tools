from __future__ import annotations
import csv, json, os, statistics, subprocess, bisect
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from micro_scalp import MicroScalpConfig, MicroScalpStrategy
from paper_engine import PaperEngine, StrategyConfig, Signal

ROOT=Path(__file__).resolve().parent
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY=r"C:\Python39\python.exe"

def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX)
    p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch.py")],env=env,text=True,capture_output=True,check=True)
    return json.loads(p.stdout.splitlines()[-1])
def iso(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def pct(n,d): return 100*n/d if d else 0.0
def quantile(xs,p):
    return statistics.quantiles(xs,n=100,method="inclusive")[max(0,min(98,int(p*100)-1))] if len(xs)>1 else (xs[0] if xs else None)
def session(t):
    h=datetime.fromtimestamp(t,timezone.utc).hour
    return "ASIA" if h<8 else "LONDON" if h<13 else "NEW_YORK" if h<21 else "OFF_HOURS"

def canonical_setups(data):
    bars=data["M5"]; end=max(x["time"] for x in bars[:-1]); start=end-184*86400
    m5=[x for x in bars if start<=x["time"]<end]; m15=data["M15"]; m15_times=[x["time"] for x in m15]; contract=data["contract"]
    strategy=MicroScalpStrategy(MicroScalpConfig(target_r=1.25)); rows=[]; rejects=defaultdict(int)
    for i,candle in enumerate(m5):
        t=candle["time"]; j=bisect.bisect_right(m15_times,t); context=m15[max(0,j-120):j]
        spread=candle["spread"]*contract["point"]; mid=candle["close"]; quote={"bid":mid-spread/2,"ask":mid+spread/2}
        signal=strategy.evaluate(context,m5[max(0,i-119):i+1],quote,contract,iso(t+300))
        if signal.direction=="NONE": rejects[signal.invalidation_reason or "no_trade"]+=1; continue
        distance=abs(float(signal.entry)-float(signal.stop_loss))
        risk=(distance/contract["tick_size"])*contract["tick_value"]*contract["min_lot"]
        setup_id=signal.fingerprint or f"micro-{t}-{i}"
        rows.append({"setup_id":setup_id,"timestamp":iso(t+300),"timestamp_epoch":t+300,"direction":signal.direction,"entry":float(signal.entry),"stop_loss":float(signal.stop_loss),"stop_distance":distance,"take_profit":float(signal.take_profit),"r_target":1.25,"tick_size":contract["tick_size"],"tick_value":contract["tick_value"],"contract_size":contract["contract_size"],"broker_min_lot":contract["min_lot"],"broker_lot_step":contract["lot_step"],"spread":spread,"required_dollar_risk_at_0_01_lot":risk,"session":session(t)})
    return start,end,rows,rejects

def static(rows):
    balances=[100,150,200,250,300,400,500,750,1000,1500,2000]
    risk_pcts=[.5,1,2]
    per={}
    for rp in risk_pcts:
        allowed=100*rp/100
        per[str(rp)]={"allowed_risk_at_100":allowed,"executable_count_at_100":sum(x["required_dollar_risk_at_0_01_lot"]<=allowed+1e-12 for x in rows),"executable_pct_at_100":pct(sum(x["required_dollar_risk_at_0_01_lot"]<=allowed+1e-12 for x in rows),len(rows))}
    matrix=[]
    for bal in balances:
        row={"balance":bal}
        for rp in risk_pcts:
            n=sum(x["required_dollar_risk_at_0_01_lot"]<=bal*rp/100+1e-12 for x in rows); row[str(rp)]={"count":n,"pct":pct(n,len(rows))}
        matrix.append(row)
    # Canonical monotonicity assertions: risk and balance can only increase fit.
    for x in rows:
        fits=[x["required_dollar_risk_at_0_01_lot"]<=100*rp/100+1e-12 for rp in risk_pcts]
        assert (not fits[0]) or (fits[1] and fits[2]), f"risk monotonicity violated: {x['setup_id']}"
        assert (not fits[1]) or fits[2], f"risk monotonicity violated: {x['setup_id']}"
    for rp in risk_pcts:
        vals=[r[str(rp)]["pct"] for r in matrix]
        assert vals==sorted(vals), f"balance monotonicity violated for {rp}%: {vals}"
    return {"risk_at_100":per,"account_size_curve":matrix}

def signal_from_row(row):
    return Signal("XAUUSDm",row["direction"],"MICRO_SCALP_SWEEP_RECLAIM",row["entry"],row["stop_loss"],row["take_profit"],.70,"M5",["canonical setup replay"],None,row["timestamp"],row["setup_id"])

def simulate(data,start,end,rows,risk_pct,balance=100.0):
    paper=PaperEngine(StrategyConfig(symbol="XAUUSDm",engine_enabled=True,risk_pct=risk_pct,max_scalp_minutes=30),os.devnull)
    bars=[x for x in data["M5"] if start<=x["time"]<end]; row_by_time={int(x["timestamp_epoch"]-300):x for x in rows}; decisions=defaultdict(int); executed=[]
    for c in bars:
        t=c["time"]; paper.process_candle(c,t+300)
        row=row_by_time.get(t)
        if row:
            spread=c["spread"]*data["contract"]["point"]; q={"bid":c["close"]-spread/2,"ask":c["close"]+spread/2}
            result=paper.evaluate(signal_from_row(row),balance,data["contract"],q,now=t+300)
            for reason in result["reasons"]: decisions[reason]+=1
            if result["decision"]=="PAPER_ENTRY": executed.append({"setup_id":row["setup_id"],"timestamp":row["timestamp"],"risk_pct":risk_pct,"calculated_lot":result["calculated_lot"],"risk_dollars":result["risk_dollars"]})
    for p in list(paper.positions):
        p.exit=bars[-1]["close"]; p.exit_reason="END_OF_PERIOD"; p.closed_at=iso(end); long=p.signal["direction"]=="LONG"; p.realized_r=(p.exit-p.entry)/abs(p.entry-p.stop_loss) if long else (p.entry-p.exit)/abs(p.entry-p.stop_loss); p.status="CLOSED"; paper.positions.remove(p); paper.closed.append(p)
    rs=[p.realized_r for p in paper.closed]; wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]; equity=peak=dd=0; streak=longest=0; durations=[]
    for p in paper.closed:
        opened=datetime.fromisoformat(p.opened_at).timestamp(); closed=datetime.fromisoformat(p.closed_at).timestamp() if p.closed_at else end; durations.append((closed-opened)/60)
    for r in rs:
        equity+=r; peak=max(peak,equity); dd=max(dd,peak-equity); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    return {"risk_pct":risk_pct,"static_executable_count":sum(x["required_dollar_risk_at_0_01_lot"]<=100*risk_pct/100+1e-12 for x in rows),"simulated_executed_trades":len(rs),"wins":len(wins),"losses":len(losses),"win_rate_pct":pct(len(wins),len(rs)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"max_drawdown_r":dd,"average_duration_minutes":sum(durations)/len(durations) if durations else 0,"longest_losing_streak":longest,"decision_reasons":dict(decisions),"executed_setups":executed,"closed_r":rs}

def main():
    data=fetch(); start,end,rows,rejects=canonical_setups(data); st=static(rows)
    risk_dist=[x["required_dollar_risk_at_0_01_lot"] for x in rows]
    distributions={str(rp):{"risk_percentage":rp,"minimum_balance":min(r/rp*100 for r in risk_dist),"p10_balance":quantile([r/rp*100 for r in risk_dist],.10),"p25_balance":quantile([r/rp*100 for r in risk_dist],.25),"median_balance":statistics.median([r/rp*100 for r in risk_dist]),"p75_balance":quantile([r/rp*100 for r in risk_dist],.75),"p90_balance":quantile([r/rp*100 for r in risk_dist],.90)} for rp in [.5,1,2]}
    simulations={str(rp):simulate(data,start,end,rows,rp,100.0) for rp in [.5,1,2]}
    thresholds={}
    for rp in [.5,1]:
        req=sorted(x["required_dollar_risk_at_0_01_lot"]/(rp/100) for x in rows)
        thresholds[str(rp)]={str(target):quantile(req,target) for target in [.10,.25,.50,.75,.90]}
    out={"period":"six_months","start":iso(start),"end":iso(end),"source":"local MT5 Exness-MT5Real27 XAUUSDm history","contract":data["contract"],"canonical_setup_count":len(rows),"canonical_setup_rejections":dict(rejects),"canonical_dataset_file":"micro_scalp_canonical_setups.csv","static_executability":st,"required_balance_distributions":distributions,"sequential_simulation":simulations,"account_size_thresholds_for_executable_fraction":thresholds,"audit_assertions":{"risk_monotonicity":True,"balance_monotonicity":True,"same_canonical_dataset_reused":True}}
    Path(ROOT/"micro_scalp_audit_results.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    fields=list(rows[0].keys()) if rows else []
    with open(ROOT/"micro_scalp_canonical_setups.csv","w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    md=["# MICRO_SCALP validation audit","","READ-ONLY / PAPER-ONLY. Strategy code and parameters were not modified.","","## Canonical dataset","",f"One signal pass produced {len(rows)} valid setups from {iso(start)} through {iso(end)}. The same canonical dataset is reused for all static 0.5%, 1%, and 2% tests. Canonical file: `micro_scalp_canonical_setups.csv`.","","## Static executability at fixed $100 balance",json.dumps(st,indent=2),"","## Sequential simulation",json.dumps(simulations,indent=2),"","## Required balance distributions",json.dumps(distributions,indent=2),"","## Account-size thresholds",json.dumps(thresholds,indent=2),"","## Cause of prior 6 vs 3 inconsistency","The prior MICRO_SCALP balance-sensitivity runs regenerated and simulated each risk scenario independently through PaperEngine. At 2% risk, the sequential stateful simulation encountered different open-position/cooldown/consecutive-loss/session constraints after earlier trades, so it reported fewer simulated trades than at 1%. Static minimum-lot feasibility is monotonic; sequential executed-trade counts need not be monotonic because state changes after fills. The canonical static counts in this audit are the authoritative executability measure.","","## Performance versus feasibility","Static executable setups answer minimum-lot feasibility only. Sequential simulation results answer what happened after PaperEngine state gates. Neither result is a profitability claim."]
    (ROOT/"micro_scalp_audit_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"canonical_setup_count":len(rows),"static_at_100":st["risk_at_100"],"simulated":{k:{m:v[m] for m in ["static_executable_count","simulated_executed_trades","win_rate_pct","profit_factor","expectancy_r"]} for k,v in simulations.items()},"thresholds":thresholds},indent=2))
if __name__=="__main__": main()
