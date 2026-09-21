from __future__ import annotations
import csv, json, math, os, statistics, subprocess, sys, bisect
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX = "/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY = r"C:\Python39\python.exe"
DEFAULT_AUDIT = str(ROOT / "backtest_audit.jsonl")

def fetch():
    env = dict(os.environ, WINEPREFIX=PREFIX)
    p = subprocess.run([WINE, WINPY, "Z:" + str(ROOT / "historical_fetch.py")], env=env, text=True, capture_output=True, check=True)
    return json.loads(p.stdout.splitlines()[-1])

def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).isoformat()
def session(ts):
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    if 0 <= h < 8: return "ASIA"
    if 8 <= h < 13: return "LONDON"
    if 13 <= h < 21: return "NEW_YORK"
    return "OFF_HOURS"
def pct(n,d): return (100*n/d) if d else 0.0
def quantile(xs, q):
    if not xs: return None
    return statistics.quantiles(xs, n=100, method="inclusive")[max(0, min(98, int(q*100)-1))] if len(xs) > 1 else xs[0]

def run_period(data, start, end, config_kwargs=None, label="period", balance=1000.0):
    from paper_engine import PaperEngine, StrategyConfig, StrategyEngine, size_position
    config_kwargs = config_kwargs or {}
    cfg = StrategyConfig(symbol="XAUUSDm", engine_enabled=True, **config_kwargs)
    strategy, paper = StrategyEngine(cfg), PaperEngine(cfg, os.devnull)
    m5 = [x for x in data["M5"] if start <= x["time"] < end]
    m15_all, h1_all = data["M15"], data["H1"]
    m15_times, h1_times = [x["time"] for x in m15_all], [x["time"] for x in h1_all]
    contract = data["contract"]
    events, rejected, setup_risks, setup_meta, setup_valid, minlot_skips = [], defaultdict(int), [], [], 0, 0
    started = start
    for i, c in enumerate(m5):
        t = c["time"]
        # The live runner processes the completed candle before evaluating it.
        paper.process_candle(c, t + 300)
        h1_end = bisect.bisect_right(h1_times, t); m15_end = bisect.bisect_right(m15_times, t)
        h1 = h1_all[max(0, h1_end-120):h1_end]
        m15 = m15_all[max(0, m15_end-120):m15_end]
        spread_price = c["spread"] * contract["point"]
        mid = c["close"]
        quote = {"bid": mid - spread_price/2, "ask": mid + spread_price/2}
        signal = strategy.evaluate(h1, m15, m5[max(0, i-119):i+1], quote, iso(t + 300))
        reason = signal.invalidation_reason if signal.direction == "NONE" else "qualified_setup"
        if signal.direction != "NONE":
            setup_valid += 1
            size = size_position(balance, float(signal.entry), float(signal.stop_loss), cfg.risk_pct, contract["tick_size"], contract["tick_value"], contract["min_lot"], contract["max_lot"], contract["lot_step"])
            min_lot_risk = (abs(float(signal.entry)-float(signal.stop_loss))/contract["tick_size"])*contract["tick_value"]*contract["min_lot"]
            setup_risks.append(min_lot_risk); setup_meta.append({"time":t,"direction":signal.direction,"setup_type":signal.setup_type,"session":session(t),"min_lot_risk_dollars":min_lot_risk})
            if size.status != "OK": minlot_skips += 1
        else: rejected[reason] += 1
        result = paper.evaluate(signal, balance, contract, quote, now=t + 300)
        if result["decision"] == "PAPER_ENTRY":
            events.append({"time": t, "session": session(t), "direction": signal.direction, "setup_type": signal.setup_type, "entry": signal.entry, "stop_loss": signal.stop_loss, "take_profit": signal.take_profit, "lot": result["calculated_lot"], "risk_dollars": result["risk_dollars"], "fingerprint": signal.fingerprint})
    # Mark any remaining positions at end-of-window as censored, not trades.
    for p in list(paper.positions):
        p.exit = m5[-1]["close"] if m5 else p.entry
        p.exit_reason = "END_OF_PERIOD"; p.closed_at = iso(end)
        long = p.signal["direction"] == "LONG"
        p.realized_r = (p.exit-p.entry)/abs(p.entry-p.stop_loss) if long else (p.entry-p.exit)/abs(p.entry-p.stop_loss)
        p.status="CLOSED"; paper.positions.remove(p); paper.closed.append(p)
    closed = []
    for p in paper.closed:
        opened = datetime.fromisoformat(p.opened_at).timestamp()
        exit_time = datetime.fromisoformat(p.closed_at).timestamp() if p.closed_at else (end if p.exit_reason == "END_OF_PERIOD" else opened)
        closed.append({"time": opened, "exit_time": exit_time, "direction": p.signal["direction"], "setup_type": p.signal["setup_type"], "entry": p.entry, "stop_loss": p.signal["stop_loss"], "take_profit": p.take_profit, "exit": p.exit, "exit_reason": p.exit_reason, "realized_r": p.realized_r, "risk_dollars": p.risk_dollars, "duration_minutes": (exit_time-opened)/60, "session": session(int(opened))})
    rs = [x["realized_r"] for x in closed]
    wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]
    eq=peak=dd=0; streak=longest=0
    for r in rs:
        eq += r; peak=max(peak,eq); dd=max(dd,peak-eq)
        streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    span_days=max((end-start)/86400, 1e-9)
    def groups(key):
        out={}
        for k in sorted(set(x[key] for x in closed)):
            z=[x["realized_r"] for x in closed if x[key]==k]; out[k]={"trades":len(z),"wins":sum(r>0 for r in z),"losses":sum(r<0 for r in z),"win_rate_pct":pct(sum(r>0 for r in z),len(z)),"average_r":sum(z)/len(z) if z else 0}
        return out
    return {"label":label,"start":iso(start),"end":iso(end),"available_m5_bars":len(m5),"trades":len(rs),"wins":len(wins),"losses":len(losses),"win_rate_pct":pct(len(wins),len(rs)),"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"average_r":sum(rs)/len(rs) if rs else 0,"max_drawdown_r":dd,"average_trade_duration_minutes":sum(x["duration_minutes"] for x in closed)/len(closed) if closed else 0,"longest_losing_streak":longest,"trades_per_day":len(rs)/span_days,"valid_setups":setup_valid,"minimum_lot_skips":minlot_skips,"pct_setups_skipped_minimum_lot":pct(minlot_skips,setup_valid),"rejections":dict(rejected),"pct_rejected_spread_or_atr":pct(rejected.get("spread_filter",0)+rejected.get("volatility_filter",0), len(m5)),"by_direction":groups("direction"),"by_setup_type":groups("setup_type"),"by_session":groups("session"),"trades_detail":closed,"events":events,"setup_risks":setup_risks,"setup_meta":setup_meta}

def main():
    data=fetch(); bars=data["M5"]; end=max(x["time"] for x in bars[:-1]); day=86400
    # Since data is current-to-past, use calendar-day windows ending at latest completed bar.
    periods=[("30_trading_days", end-42*day, end), ("90_trading_days", end-126*day, end), ("6_months", end-184*day, end)]
    old = json.loads((ROOT/"backtest_results.json").read_text()) if os.environ.get("REUSE_SENSITIVITY") and (ROOT/"backtest_results.json").exists() else None
    results={"source":"local MT5 Exness-MT5Real27 XAUUSDm history","contract":data["contract"],"account":data["account"],"periods":{}}
    for label,s,e in periods: results["periods"][label]=run_period(data,s,e,label=label,balance=float(data["account"].get("equity") or 1000.0))
    # Small sensitivity grid on 90 trading days only.
    sens=[]; base_s,end_s=periods[1][1],periods[1][2]
    if old: sens = old.get("sensitivity_90d", [])
    for rr in ([] if old else [1.2,1.5,2.0]):
        for atr_mult,label in [(1.0,"current"),(0.75,"0.75x_current"),(1.25,"1.25x_current")]:
            for rsi_on,rsi_label in [(True,"current"),(False,"disabled")]:
                q=run_period(data,base_s,end_s,{"reward_risk_target":rr,"atr_buffer_multiplier":atr_mult,"rsi_confirmation":rsi_on},label=f"RR{rr}_ATR{label}_RSI{rsi_label}",balance=float(data["account"].get("equity") or 1000.0))
                sens.append({k:q[k] for k in ["label","trades","win_rate_pct","profit_factor","expectancy_r","average_r","max_drawdown_r","pct_setups_skipped_minimum_lot"]})
    results["sensitivity_90d"]=sens
    # Walk forward: older 60 calendar days reference, following 30 calendar days validation; parameters unchanged.
    wf_train_end=end-30*day; wf_train_start=wf_train_end-60*day
    results["walk_forward"]=(old.get("walk_forward") if old else {"train_reference":run_period(data,wf_train_start,wf_train_end,label="train_reference_60d",balance=float(data["account"].get("equity") or 1000.0)),"validation":run_period(data,wf_train_end,end,label="validation_following_30d",balance=float(data["account"].get("equity") or 1000.0)),"parameters_changed":False})
    # Risk realism across all valid setups from 6-month window, independent of entries skipped by paper gates.
    p=results["periods"]["6_months"]; risks=[]; intended=float(data["account"].get("equity") or 1000.0)*0.005
    risks = list(p["setup_risks"])
    results["risk_realism"]={"account_equity_used":float(data["account"].get("equity") or 1000.0),"intended_risk_dollars_at_0.5pct":intended,"minimum_lot":data["contract"]["min_lot"],"median_risk_dollars_at_0.01_lot":statistics.median(risks) if risks else None,"p90_risk_dollars_at_0.01_lot":quantile(risks,0.9),"pct_valid_setups_0.01_exceeds_intended_risk":pct(sum(r>intended for r in risks),len(risks)),"minimum_practical_account_size_at_0.5pct":(quantile(risks,0.9)/0.005 if risks else None),"valid_setup_sample":len(risks)}
    codes=[]
    if (results["risk_realism"]["pct_valid_setups_0.01_exceeds_intended_risk"] or 0)>50: codes.append("D")
    if all(results["periods"][k]["trades_per_day"] < 0.1 for k in results["periods"]): codes.append("B")
    results["recommendation_codes"]=codes or ["A"]
    Path(ROOT/"backtest_results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    with open(ROOT/"backtest_trades.csv","w",newline="",encoding="utf-8") as f:
        fields=["period","time","exit_time","direction","setup_type","entry","stop_loss","take_profit","exit","exit_reason","realized_r","risk_dollars","duration_minutes","session"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for label,p in results["periods"].items():
            for x in p["trades_detail"]: w.writerow({"period":label,**x})
    md=["# XAUUSDm historical paper validation","","READ-ONLY / PAPER-ONLY. No live order tools or live execution were used.","","## Data and methodology","","Source: local Exness-MT5Real27 MT5 history for XAUUSDm. M5 execution with completed M15/H1 context. Signals use only candles through the decision candle; conservative same-candle SL priority is inherited from `PaperEngine`. Historical bid/ask is reconstructed from the candle close and recorded bar spread because MT5 bar history does not include bid/ask ticks.","","## Period results"]
    for label,p in results["periods"].items(): md += [f"### {label}",f"- Trades {p['trades']} ({p['wins']} wins / {p['losses']} losses); win rate {p['win_rate_pct']:.1f}%",f"- Profit factor {p['profit_factor']}; expectancy {p['expectancy_r']:.3f}R; average R {p['average_r']:.3f}R; max drawdown {p['max_drawdown_r']:.3f}R",f"- Avg duration {p['average_trade_duration_minutes']:.1f} min; longest losing streak {p['longest_losing_streak']}; trades/day {p['trades_per_day']:.2f}",f"- Valid setups {p['valid_setups']}; min-lot skips {p['pct_setups_skipped_minimum_lot']:.1f}%; spread/ATR rejects {p['pct_rejected_spread_or_atr']:.1f}% of M5 decision bars","- Direction: "+json.dumps(p["by_direction"]),"- Setup: "+json.dumps(p["by_setup_type"]),"- Session: "+json.dumps(p["by_session"]),""]
    md += ["## Risk realism at 0.5%",json.dumps(results["risk_realism"],indent=2),"","## Sensitivity", "Small 90-day grid only; no parameter was selected from validation data.",json.dumps(results["sensitivity_90d"],indent=2),"","## Walk-forward",json.dumps({k:{x:y for x,y in v.items() if x not in ('trades_detail','events')} if isinstance(v,dict) else v for k,v in results['walk_forward'].items()},indent=2),"","## Recommendation","Recommendation is evidence-limited to these local-history results. No profitability claim is made; review the JSON/CSV for full breakdowns.","Codes: "+", ".join(results["recommendation_codes"])]
    (ROOT/"backtest_summary.md").write_text("\n".join(md),encoding="utf-8")
    print(json.dumps({"periods":{k:{x:v[x] for x in ["trades","wins","losses","win_rate_pct","profit_factor","expectancy_r","average_r"]} for k,v in results["periods"].items()},"risk_realism":results["risk_realism"],"files":["backtest_results.json","backtest_trades.csv","backtest_summary.md"]},indent=2))
if __name__ == "__main__": main()
