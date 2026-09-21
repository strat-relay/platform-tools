from __future__ import annotations
import argparse, hashlib, json, os, signal, statistics, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from liquidity_displacement import LiquidityDisplacementStrategy, LiquidityDisplacementConfig
from paper_engine import _candle, atr
from strategy_report_format import format_standard_report

ROOT=Path(__file__).resolve().parent
STATE=ROOT/"liquidity_displacement_forward_state.json"; EVENTS=ROOT/"liquidity_displacement_forward.jsonl"; DAILY=ROOT/"liquidity_displacement_daily.jsonl"; SUMMARY=ROOT/"liquidity_displacement_forward_summary.md"; MANIFEST=ROOT/"liquidity_displacement_forward_manifest.json"; PIDFILE=ROOT/"liquidity_displacement_forward.pid"; HEARTBEAT=ROOT/"liquidity_displacement_forward.heartbeat.json"; STOP=Path("/tmp/liquidity-displacement-paper.stop")
WINE=os.environ.get("MT5_WINE_BIN", "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"); PREFIX=os.environ.get("MT5_WINEPREFIX", "/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"); WINPY=os.environ.get("MT5_WINPY", r"C:\Python39\python.exe"); READ_ONCE_PATH=Path(os.environ.get("MT5_READ_ONCE_PATH", str(ROOT/"mt5_read_once.py"))); SYMBOL="XAUUSDm"; PAPER_TITLE="LIQUIDITY_DISPLACEMENT_SCALP_V1 — FORWARD PAPER"; SLIPPAGE=0.02
CFG=LiquidityDisplacementConfig(target_r=1.25,max_hold_minutes=120,max_retrace_candles=3,max_structure_break_candles=5,min_body_atr=.5,min_body_median_multiple=1.,min_close_location=.6,atr_buffer_fraction=.1)
ENTRY_FRACTION=.50
def now():return datetime.now(timezone.utc).isoformat()
def source_hash():return hashlib.sha256((ROOT/"liquidity_displacement.py").read_bytes()).hexdigest()
def manifest():return {"version":"LIQUIDITY_DISPLACEMENT_SCALP_V1","target_r":1.25,"source_sha256":source_hash(),"config":CFG.__dict__|{"symbol":SYMBOL},"entry_rules":"sweep -> reclaim -> displacement -> micro shift -> 50% displacement retracement","stop_rules":"sweep extreme plus max(0.10 ATR, 1.25x spread, broker stop minimum)","retracement_rules":"maximum 3 completed M5 candles; no chase; M5 only; M15 context; M1 disabled","max_hold_minutes":120,"slippage_assumption_price":SLIPPAGE}
def load_state():
    if STATE.exists():s=json.loads(STATE.read_text())
    else:s={"version":manifest(),"started_at":None,"running":False,"last_candle":None,"last_mt5_data_timestamp":None,"last_poll_timestamp":None,"signals":{},"fills":[],"closes":[],"checkpoints":[],"gold_symbols":[],"source_sha256_at_start":None,"checkpoint_thresholds":[]}
    for k,v in {"signals":{},"decision_telemetry":{},"fills":[],"closes":[],"checkpoints":[],"checkpoint_thresholds":[],"last_successful_mt5_read":None,"last_read_error":None}.items():s.setdefault(k,v)
    return s
def atomic_write(path,value):
    path=Path(path); fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=str(path.parent));
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:json.dump(value,f,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
def save(s):atomic_write(STATE,s)
def read_json(path,default=None):
    try:return json.loads(Path(path).read_text()) if Path(path).exists() else default
    except (OSError,json.JSONDecodeError):return default
def write_heartbeat(s,status="RUNNING",interval=15,finalized_at=None):
    hb={"pid":os.getpid(),"status":status,"timestamp":now(),"last_successful_mt5_read":s.get("last_successful_mt5_read"),"last_processed_m5_candle":s.get("last_candle"),"poll_interval_seconds":interval}
    if finalized_at:hb["finalized_at"]=finalized_at
    atomic_write(HEARTBEAT,hb)
def process_alive(pid):
    try:os.kill(int(pid),0); return True
    except (OSError,TypeError,ValueError):return False
def acquire_lock(s):
    old=read_json(PIDFILE)
    if old and process_alive(old.get("pid")):
        print(f"Forward runner already active\nPID: {old.get('pid')}\nStarted: {old.get('started_at')}"); return False
    if PIDFILE.exists():PIDFILE.unlink()
    atomic_write(PIDFILE,{"pid":os.getpid(),"started_at":s.get("started_at") or now()}); return True
def release_lock():
    old=read_json(PIDFILE)
    if not old or int(old.get("pid",-1))==os.getpid():
        try:PIDFILE.unlink()
        except FileNotFoundError:pass
def runtime_status(s):
    lock=read_json(PIDFILE); hb=read_json(HEARTBEAT,{}) or {}; pid=lock.get("pid") if lock else hb.get("pid"); alive=process_alive(pid) if pid else False; age=(time.time()-ts_value(hb.get("timestamp"))) if hb.get("timestamp") else None
    return ("ACTIVE" if alive and (age is None or age<=60) else "STALE" if alive else "DOWN"),hb,lock
def event(s,kind,payload):
    row={"timestamp":now(),"event":kind,**payload};
    with EVENTS.open("a",encoding="utf-8") as f:f.write(json.dumps(row,separators=(",",":"))+"\n")
    save(s)
def read_once():
    env=dict(os.environ,WINEPREFIX=PREFIX); p=subprocess.run([WINE,WINPY,"Z:"+str(READ_ONCE_PATH),SYMBOL],env=env,text=True,capture_output=True,timeout=35,check=True); data=json.loads(p.stdout.splitlines()[-1]); actual=data.get("contract",{}).get("symbol")
    if actual != SYMBOL: raise RuntimeError(f"MT5 symbol mismatch: configured {SYMBOL}, received {actual}")
    return data
def signal_features(data):
    m5=data["M5"][:-1]; m15=data["M15"][:-1]; h1=data["H1"][:-1]; q=data["quote"]; c=data["contract"]
    return m5,m15,h1,q,c
def price_spread_points(q,c):
    points=float(q.get("spread_points",c.get("spread_points",0)) or 0)
    return points, points*float(c.get("point",c.get("tick_size",0.001)) or 0.001)
def pct(values,p):
    xs=sorted(float(x) for x in values if x is not None)
    if not xs:return 0.0
    if len(xs)==1:return xs[0]
    k=(len(xs)-1)*p; lo=int(k); hi=min(lo+1,len(xs)-1); return xs[lo]+(xs[hi]-xs[lo])*(k-lo)
def session(ts):
    h=(datetime.fromisoformat(ts).hour if isinstance(ts,str) and "T" in ts else datetime.fromtimestamp(float(ts),timezone.utc).hour)
    if 0<=h<8:return "Asia"
    if 8<=h<13:return "London"
    if 13<=h<17:return "London/New York overlap"
    if 17<=h<22:return "New York"
    return "Other"
def outcome_stats(records):
    closed=[r for r in records if r.get("outcome")]
    rs=[float(r.get("r_theoretical",0)) for r in closed]; wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]
    return {"wins":sum(r.get("outcome")=="WIN" for r in closed),"losses":sum(r.get("outcome")=="LOSS" for r in closed),"time_exits":sum(r.get("outcome")=="TIME_EXIT" for r in closed),"daily_r":sum(rs),"cumulative_forward_r":sum(rs),"win_rate_pct":100*len(wins)/(len(wins)+len(losses)) if wins or losses else 0,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0}
def drawdown(records):
    eq=peak=dd=0
    for r in sorted([x for x in records if x.get("outcome")],key=lambda x:x.get("simulated_close_timestamp") or x.get("detected_at")):
        eq+=float(r.get("r_theoretical",0)); peak=max(peak,eq); dd=max(dd,peak-eq)
    return dd
def drift_warnings(summary, prior_summaries=None, all_records=None):
    prior_summaries=prior_summaries or []; warnings=[]; n=summary.get("setups_detected",0); closed=summary.get("wins",0)+summary.get("losses",0)+summary.get("time_exits",0)
    if n>=10 and abs(summary.get("fill_rate_pct",0)-42.4)>15: warnings.append({"code":"FILL_RATE_DRIFT","message":"Daily fill rate differs from 42.4% historical reference by more than 15 percentage points."})
    if closed>=10 and summary.get("expectancy_r",0)<0: warnings.append({"code":"EXPECTANCY_DRIFT","message":"Daily theoretical expectancy is negative; informational only."})
    if closed>=10 and summary.get("profit_factor") is not None and summary["profit_factor"]<1: warnings.append({"code":"PROFIT_FACTOR_DRIFT","message":"Daily profit factor is below 1.0; informational only."})
    if summary.get("max_drawdown_r",0)>4.04*1.5: warnings.append({"code":"DRAWDOWN_WARNING","message":"Forward drawdown is materially above the historical validation reference."})
    prior_spreads=[x.get("median_spread_price") for x in prior_summaries if x.get("median_spread_price")]
    if summary.get("median_spread_price") and len(prior_spreads)>=2 and summary["median_spread_price"]>statistics.median(prior_spreads)*1.25: warnings.append({"code":"SPREAD_DRIFT","message":"Median spread is more than 25% above prior forward-paper days."})
    if summary.get("median_stop_distance") and prior_summaries:
        ps=[x.get("median_stop_distance") for x in prior_summaries if x.get("median_stop_distance")]
        if ps and (summary["median_stop_distance"]>statistics.median(ps)*1.5 or summary["median_stop_distance"]<statistics.median(ps)*.5): warnings.append({"code":"STOP_DISTANCE_DRIFT","message":"Median stop distance differs materially from prior forward-paper days."})
    prior_duration=[x.get("median_trade_duration_minutes") for x in prior_summaries if x.get("median_trade_duration_minutes")]
    if summary.get("median_trade_duration_minutes") and prior_duration and (summary["median_trade_duration_minutes"]>statistics.median(prior_duration)*1.5 or summary["median_trade_duration_minutes"]<statistics.median(prior_duration)*.5): warnings.append({"code":"DURATION_DRIFT","message":"Median trade duration differs materially from prior forward-paper days."})
    prior_sessions=[x.get("setups_by_session",{}) for x in prior_summaries if x.get("setups_by_session")]; current_sessions=summary.get("setups_by_session",{})
    if prior_sessions and current_sessions:
        prior_mode=max(set(k for x in prior_sessions for k in x),key=lambda k:sum(x.get(k,0) for x in prior_sessions)); current_mode=max(current_sessions,key=current_sessions.get)
        if prior_mode!=current_mode:warnings.append({"code":"SESSION_DISTRIBUTION_DRIFT","message":"Dominant setup session differs from prior forward-paper days."})
    prior_directions=[x.get("direction_distribution",{}) for x in prior_summaries if x.get("direction_distribution")]; current_directions=summary.get("direction_distribution",{})
    if prior_directions and current_directions:
        prior_long=sum(x.get("LONG",0) for x in prior_directions)/max(1,sum(sum(x.values()) for x in prior_directions)); current_long=current_directions.get("LONG",0)/max(1,sum(current_directions.values()))
        if abs(current_long-prior_long)>.35:warnings.append({"code":"DIRECTION_DISTRIBUTION_DRIFT","message":"LONG/SHORT mix differs materially from prior forward-paper days."})
    return warnings
def follow_unfilled(rec,m5):
    if rec.get("status") not in ("UNFILLED_EXPIRED","INVALIDATED"):return
    start=int(rec.get("displacement_index",rec.get("bar_index",0)))+1; entry=float(rec["entry_theoretical"]); stop=float(rec["stop_loss"]); target=float(rec.get("target_theoretical") or (entry+(entry-stop)*CFG.target_r if rec["direction"]=="LONG" else entry-(stop-entry)*CFG.target_r)); long=rec["direction"]=="LONG"; bars=[x for x in m5 if int(x["time"])>=int(m5[min(start,len(m5)-1)]["time"])]
    if not bars:return
    favorable=[(float(x["high"])-entry) if long else (entry-float(x["low"])) for x in bars]; adverse=[(entry-float(x["low"])) if long else (float(x["high"])-entry) for x in bars]
    rec["unfilled_followup"]={"max_favorable_excursion_r":max(favorable)/float(rec["stop_distance"]),"max_adverse_excursion_r":max(adverse)/float(rec["stop_distance"]),"target_eventually_reached":any(float(x["high"])>=target if long else float(x["low"])<=target for x in bars),"stop_eventually_reached":any(float(x["low"])<=stop if long else float(x["high"])>=stop for x in bars),"closest_entry_distance":min(abs((float(x["low"]) if long else float(x["high"]))-entry) for x in bars),"minimum_retracement_achieved":max(favorable),"last_followup_timestamp":datetime.fromtimestamp(int(bars[-1]["time"])+300,timezone.utc).isoformat()}
def detect_gap(s,data):
    completed=data.get("M5",[])[:-1]
    if not completed or s.get("last_candle") is None:return None
    current=int(completed[-1]["time"]); previous=int(s["last_candle"]); missing=max(0,(current-previous)//300-1)
    if missing<=0:return None
    reason="MT5_UNAVAILABLE" if s.get("last_read_error") else "RUNNER_DOWN" if s.get("last_poll_timestamp") and time.time()-ts_value(s["last_poll_timestamp"])>max(120,CFG.max_hold_minutes*60) else "MAC_SLEEP" if s.get("last_poll_timestamp") and time.time()-ts_value(s["last_poll_timestamp"])>300 else "UNKNOWN"
    return {"last_live_candle":datetime.fromtimestamp(previous,timezone.utc).isoformat(),"current_candle":datetime.fromtimestamp(current,timezone.utc).isoformat(),"gap_start":datetime.fromtimestamp(previous+300,timezone.utc).isoformat(),"gap_end":datetime.fromtimestamp(current-300,timezone.utc).isoformat(),"missing_m5_candles":missing,"reason":reason,"detected_at":now()}
def replay_gap(s,data,gap):
    completed=data.get("M5",[])[:-1]; by_time={int(x["time"]):i for i,x in enumerate(completed)}; start=int(datetime.fromisoformat(gap["gap_start"]).timestamp()); end=int(datetime.fromisoformat(gap["gap_end"]).timestamp()); recovered=[]
    for ts in range(start,end+1,300):
        idx=by_time.get(ts)
        if idx is None:continue
        if idx+1>=len(data.get("M5",[])):continue
        raw=dict(data); raw["M5"]=data["M5"][:idx+2]; raw["M15"]=[x for x in data.get("M15",[]) if int(x["time"])<=ts+900]; raw["H1"]=[x for x in data.get("H1",[]) if int(x["time"])<=ts+3600]
        process(s,raw,"GAP_RECOVERY"); recovered.append(ts)
    gap["recovered_candles"]=len(recovered); gap["recovery_status"]="COMPLETE" if len(recovered)==gap["missing_m5_candles"] else "PARTIAL"; event(s,"DATA_GAP_RECOVERY",{"gap":gap,"source":"GAP_RECOVERY"}); return gap
def metric_block(records):
    setups=len(records); fills=sum(bool(r.get("simulated_fill_timestamp")) for r in records); closed=[r for r in records if r.get("outcome")]; rs=[float(r.get("r_theoretical",0)) for r in closed]; wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]; return {"setups":setups,"fills":fills,"wins":sum(r.get("outcome")=="WIN" for r in closed),"losses":sum(r.get("outcome")=="LOSS" for r in closed),"time_exits":sum(r.get("outcome")=="TIME_EXIT" for r in closed),"expectancy_r":sum(rs)/len(rs) if rs else 0,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"cumulative_r":sum(rs),"max_drawdown_r":drawdown(records)}
def settle_filled_record(s,rec,m5,q,c,source):
    if rec.get("status")!="FILLED" or rec.get("outcome") is not None:return
    long=rec["direction"]=="LONG"; entry=float(rec.get("entry_theoretical",rec.get("entry_realistic"))); entry_real=float(rec.get("entry_realistic",entry)); sl=float(rec["stop_loss"]); tp=float(rec["target_theoretical"]); fill_epoch=datetime.fromisoformat(rec["simulated_fill_timestamp"]).timestamp()
    bars_after_fill=[x for x in m5 if int(x["time"])+300>=fill_epoch]
    mfe_price=0.0; mae_price=0.0
    for bar in bars_after_fill:
        high=float(bar["high"]); low=float(bar["low"]); hit_sl=low<=sl if long else high>=sl; hit_tp=high>=tp if long else low<=tp; close_epoch=int(bar["time"])+300; elapsed=close_epoch-fill_epoch; close_ts=datetime.fromtimestamp(close_epoch,timezone.utc).isoformat(); bar_spread=price_spread_points({"spread_points":bar.get("spread",0)},c)[1]
        favorable=(high-entry) if long else (entry-low); adverse=(entry-low) if long else (high-entry); mfe_price=max(mfe_price,favorable); mae_price=max(mae_price,adverse)
        if elapsed>=CFG.max_hold_minutes*60:
            exit_theoretical=float(bar["close"]); exit_realistic=exit_theoretical-bar_spread/2 if long else exit_theoretical+bar_spread/2; rec["outcome"]="TIME_EXIT"; rec["status"]="TIME_EXIT"
        elif hit_sl or hit_tp:
            exact=sl if hit_sl else tp; exit_theoretical=exact; exit_realistic=(exact-SLIPPAGE) if long else (exact+SLIPPAGE); rec["outcome"]="LOSS" if hit_sl else "WIN"; rec["status"]="STOPPED" if hit_sl else "TARGET_HIT"
        else:continue
        rec["spread_at_exit_points"]=float(bar.get("spread",0)); rec["spread_at_exit_price"]=bar_spread; rec["exit_price_theoretical"]=exit_theoretical; rec["exit_price_realistic"]=exit_realistic; rec["simulated_close_timestamp"]=close_ts; rec["duration_minutes"]=max(0.0,elapsed/60); rec["exit_reason"]="TIME_EXIT" if rec["outcome"]=="TIME_EXIT" else rec["status"]; rec["mfe_price"]=mfe_price; rec["mae_price"]=mae_price; rec["mfe_r"]=mfe_price/float(rec["stop_distance"]); rec["mae_r"]=mae_price/float(rec["stop_distance"]); rec["r_theoretical"]=((exit_theoretical-entry)/float(rec["stop_distance"])) if long else ((entry-exit_theoretical)/float(rec["stop_distance"])); rec["r_realistic"]=((exit_realistic-entry_real)/float(rec["stop_distance"])) if long else ((entry_real-exit_realistic)/float(rec["stop_distance"])); event(s,rec["status"],{"setup_id":rec["setup_id"],"r_theoretical":rec["r_theoretical"],"r_realistic":rec["r_realistic"],"exit_reason":rec["exit_reason"],"source":source}); return
def account_feasibility(records):
    out={}
    for pct_key in ("required_balance_0_5","required_balance_1_0","required_balance_2_0"):
        vals=[float(r[pct_key]) for r in records if r.get(pct_key) is not None]; out[pct_key]={"minimum":min(vals) if vals else 0,"p25":pct(vals,.25),"median":pct(vals,.5),"p75":pct(vals,.75)}
    for balance in (100,150,250,500,750,1000):
        out[str(balance)]={}
        for risk_key,pct_key in (("0.5%","required_balance_0_5"),("1%","required_balance_1_0"),("2%","required_balance_2_0")):
            vals=[r for r in records if r.get(pct_key) is not None]; out[str(balance)][risk_key]=100*sum(float(r[pct_key])<=balance for r in vals)/len(vals) if vals else 0
    return out
def daily_aggregate(s,date_text,daily_records=None):
    records=list(s.get("signals",{}).values()); selected=[r for r in records if (r.get("detected_at") or "")[:10]==date_text]; closed=[r for r in records if (r.get("simulated_close_timestamp") or "")[:10]==date_text]
    perf=outcome_stats(closed); spreads=[float(r.get("spread_at_detection_price",r.get("spread_at_detection",0))) for r in selected]; stops=[float(r["stop_distance"]) for r in selected if r.get("stop_distance") is not None]; risks=[float(r["risk_at_001"]) for r in selected if r.get("risk_at_001") is not None]; durations=[(datetime.fromisoformat(r["simulated_close_timestamp"]).timestamp()-datetime.fromisoformat(r["simulated_fill_timestamp"]).timestamp())/60 for r in closed if r.get("simulated_close_timestamp") and r.get("simulated_fill_timestamp")]; ratios=[float(r.get("spread_at_detection",0))/float(r["stop_distance"]) for r in selected if r.get("stop_distance")]
    sessions={}; fills_sessions={}; directions={"LONG":0,"SHORT":0}
    for r in selected:sessions[session(r.get("sweep_timestamp",r.get("detected_at")))] = sessions.get(session(r.get("sweep_timestamp",r.get("detected_at"))),0)+1; directions[r.get("direction")]=directions.get(r.get("direction"),0)+1
    for r in [x for x in records if (x.get("simulated_fill_timestamp") or "")[:10]==date_text]: fills_sessions[session(r["simulated_fill_timestamp"])]=fills_sessions.get(session(r["simulated_fill_timestamp"]),0)+1
    out={"date":date_text,"setups_detected":len(selected),"retracement_fills":sum(bool(r.get("status") in ("FILLED","TARGET_HIT","STOPPED","TIME_EXIT") or r.get("outcome")) for r in selected),"expired_unfilled":sum(r.get("status")=="UNFILLED_EXPIRED" for r in selected),"invalidated":sum(r.get("status")=="INVALIDATED" for r in selected),"fill_rate_pct":100*sum(bool(r.get("simulated_fill_timestamp")) for r in selected)/len(selected) if selected else 0,"wins":perf["wins"],"losses":perf["losses"],"time_exits":perf["time_exits"],"daily_r":perf["daily_r"],"cumulative_forward_r":sum(float(r.get("r_theoretical",0)) for r in records if r.get("outcome") and (r.get("simulated_close_timestamp") or "")[:10]<=date_text),"win_rate_pct":perf["win_rate_pct"],"profit_factor":perf["profit_factor"],"expectancy_r":perf["expectancy_r"],"max_drawdown_r":drawdown(records),"average_spread_price":statistics.mean(spreads) if spreads else 0,"median_spread_price":pct(spreads,.5),"maximum_spread_price":max(spreads) if spreads else 0,"average_spread_stop_ratio":statistics.mean(ratios) if ratios else 0,"median_stop_distance":pct(stops,.5),"median_risk_at_001":pct(risks,.5),"average_simulated_slippage":statistics.mean([float(r.get("slippage_assumption_price",0)) for r in closed]) if closed else 0,"theoretical_r":sum(float(r.get("r_theoretical",0)) for r in closed),"realistic_r":sum(float(r.get("r_realistic",0)) for r in closed),"average_trade_duration_minutes":statistics.mean(durations) if durations else 0,"median_trade_duration_minutes":pct(durations,.5),"setups_by_session":sessions,"fills_by_session":fills_sessions,"direction_distribution":directions,"account_feasibility":account_feasibility(selected)}
    out["warnings"]=drift_warnings(out,daily_records if daily_records is not None else read_daily_records()); return out
def read_daily_records():
    if not DAILY.exists():return []
    rows=[]
    for line in DAILY.read_text().splitlines():
        try:rows.append(json.loads(line))
        except json.JSONDecodeError:pass
    return rows
def persist_daily(summary):
    rows=read_daily_records()
    if any(x.get("date")==summary.get("date") for x in rows):return False
    with DAILY.open("a",encoding="utf-8") as f:f.write(json.dumps(summary,separators=(",",":"))+"\n")
    return True
def health(s):
    today=datetime.now(timezone.utc).date().isoformat(); d=daily_aggregate(s,today)
    runtime,hb,lock=runtime_status(s)
    return {"running":s.get("running"),"runner_status":runtime,"runner_pid":lock.get("pid") if lock else hb.get("pid"),"version":s.get("version",{}).get("version"),"source_sha256":s.get("version",{}).get("source_sha256"),"last_mt5_data_timestamp":s.get("last_mt5_data_timestamp"),"last_poll_timestamp":s.get("last_poll_timestamp"),"heartbeat":hb,"tracked_signals":len(s.get("signals",{})),"decision_telemetry_candidates":len(s.get("decision_telemetry",{})),"current_paper_positions":sum(x.get("status")=="FILLED" and not x.get("outcome") for x in s.get("signals",{}).values()),"today":d,"cumulative_forward_fills":sum(bool(x.get("simulated_fill_timestamp")) for x in s.get("signals",{}).values()),"cumulative_r":sum(float(x.get("r_theoretical",0)) for x in s.get("signals",{}).values() if x.get("outcome")),"current_drawdown_r":drawdown(list(s.get("signals",{}).values())),"last_gap":s.get("last_gap"),"active_drift_warnings":d.get("warnings",[]),"kill_switch":STOP.exists()}
def trade_records(s,date_text=None,limit=None):
    rows=[x for x in s.get("signals",{}).values() if x.get("simulated_fill_timestamp")]
    rows=sorted(rows,key=lambda x:x.get("simulated_fill_timestamp") or "")
    if date_text:rows=[x for x in rows if (x.get("simulated_fill_timestamp") or "")[:10]==date_text]
    return rows[-limit:] if limit and limit>0 else rows
def trade_summary(records,all_records):
    closed=[x for x in records if x.get("outcome")]; wins=sum(x.get("outcome")=="WIN" for x in closed); losses=sum(x.get("outcome")=="LOSS" for x in closed); times=sum(x.get("outcome")=="TIME_EXIT" for x in closed); rs=[float(x.get("r_theoretical",0)) for x in closed]; gross_profit=sum(x for x in rs if x>0); gross_loss=sum(x for x in rs if x<0)
    return {"total_fills":len(records),"open_paper_positions":sum(x.get("status")=="FILLED" and not x.get("outcome") for x in records),"wins":wins,"losses":losses,"time_exits":times,"win_rate_pct":100*wins/(wins+losses) if wins+losses else 0,"cumulative_r":sum(rs),"expectancy_r":sum(rs)/len(rs) if rs else None,"profit_factor":gross_profit/abs(gross_loss) if gross_loss else None,"setups_detected":len(all_records),"waiting_retrace":sum(x.get("status")=="WAITING_FOR_RETRACE" for x in all_records),"expired_unfilled":sum(x.get("status")=="UNFILLED_EXPIRED" for x in all_records)}
def render_trades(s,date_text=None,limit=None):
    def price_text(value):
        try:
            return f"{float(value):9.3f}"
        except (TypeError, ValueError):
            return f"{'—':>9}"

    def field_text(value, default="?"):
        return default if value is None else str(value)

    all_records=list(s.get("signals",{}).values()); scoped=[x for x in all_records if not date_text or (x.get("detected_at") or "")[:10]==date_text]; records=trade_records(s,date_text,limit); lines=[PAPER_TITLE.replace("FORWARD PAPER","PAPER TRADES"),"="*112]
    if not records:
        lines += ["No paper trades have filled yet.",f"Setups detected: {len(scoped)}",f"Waiting for retracement: {sum(x.get('status')=='WAITING_FOR_RETRACE' for x in scoped)}",f"Expired unfilled: {sum(x.get('status')=='UNFILLED_EXPIRED' for x in scoped)}"]
    else:
        lines.append("# | Time             | Side  | Entry     | Exit      | SL        | TP        | Result | R       | Duration | Exit Reason")
        for i,r in enumerate(records,1):
            fill_time=datetime.fromisoformat(r["simulated_fill_timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
            exit_time=r.get("simulated_close_timestamp"); duration="OPEN"; exit_price=r.get("exit_price_realistic",r.get("exit_price_theoretical","—"))
            if exit_time and r.get("simulated_fill_timestamp"):
                duration=f"{(datetime.fromisoformat(exit_time)-datetime.fromisoformat(r['simulated_fill_timestamp'])).total_seconds()/60:.0f}m"
            r_value=r.get("r_theoretical"); r_text="N/A" if r_value is None else f"{float(r_value):+.2f}R"
            lines.append(f"{i} | {fill_time} | {field_text(r.get('direction'),' ?'):5} | {price_text(r.get('entry_realistic',r.get('entry_theoretical',0)))} | {price_text(exit_price)} | {price_text(r.get('stop_loss',0))} | {price_text(r.get('target_theoretical',0))} | {field_text(r.get('outcome'),'OPEN'):6} | {r_text:7} | {duration:8} | {field_text(r.get('status'),'')}")
    summary=trade_summary(records,scoped); lines += ["","Total fills:        "+str(summary["total_fills"]),"Open paper positions: "+str(summary["open_paper_positions"]),"Wins:                "+str(summary["wins"]),"Losses:              "+str(summary["losses"]),"Time exits:          "+str(summary["time_exits"]),f"Win rate:            {summary['win_rate_pct']:.1f}%",f"Cumulative R:        {summary['cumulative_r']:.2f}",f"Expectancy R:        {summary['expectancy_r'] if summary['expectancy_r'] is not None else 'N/A'}",f"Profit factor:       {summary['profit_factor'] if summary['profit_factor'] is not None else 'N/A'}"]
    return "\n".join(lines)
def event_rows(limit=5):
    if not EVENTS.exists():return []
    rows=[]
    for line in EVENTS.read_text().splitlines()[-limit:]:
        try:rows.append(json.loads(line))
        except json.JSONDecodeError:pass
    return rows
def ts_value(value):
    if value is None:return None
    if isinstance(value,(int,float)):return float(value)
    return datetime.fromisoformat(value).timestamp()
def age_text(seconds):
    if seconds is None:return "N/A"
    seconds=max(0,int(seconds))
    if seconds<60:return f"{seconds}s ago"
    return f"{seconds//60}m {seconds%60:02d}s ago"
def event_clock(value):
    try:return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except (TypeError,ValueError):return "--:--:--"
def watch_snapshot(s,data=None,now_ts=None):
    now_ts=now_ts or time.time(); data=data or {}; quote=data.get("quote") or {}; contract=data.get("contract") or {}; signals=list(s.get("signals",{}).values()); closed=[x for x in signals if x.get("outcome")]; fills=[x for x in signals if x.get("simulated_fill_timestamp")]; waiting=[x for x in signals if x.get("status")=="WAITING_FOR_RETRACE"]; positions=[x for x in signals if x.get("status")=="FILLED" and not x.get("outcome")]
    quote_age=now_ts-ts_value(quote.get("time")) if quote.get("time") else None; m5=data.get("M5") or []; last_bar=(int(m5[-2]["time"])+300) if len(m5)>=2 else None; m5_age=now_ts-last_bar if last_bar else None; heartbeat_age=now_ts-ts_value(s.get("last_poll_timestamp")) if s.get("last_poll_timestamp") else None
    if not data: bridge="DISCONNECTED"; mt5_state="DISCONNECTED"
    elif quote_age is not None and quote_age<=60 and m5_age is not None and m5_age<=600: bridge="CONNECTED"; mt5_state="LIVE"
    else: bridge="CONNECTED"; mt5_state="STALE"
    today=daily_aggregate(s,datetime.now(timezone.utc).date().isoformat()); filled_count=len(fills); next_checkpoint=next((x for x in (25,50,100) if filled_count<x),None)
    warnings=today.get("warnings",[]); if_stale=[]
    if quote_age is not None and quote_age>60: if_stale.append("MT5 DATA STALE")
    if m5_age is not None and m5_age>600: if_stale.append("M5 DATA STALE")
    runtime,heartbeat,lock=runtime_status(s)
    return {"version":s.get("version",{}).get("version","UNKNOWN"),"hash":s.get("version",{}).get("source_sha256",""),"bridge":bridge,"mt5_state":mt5_state,"quote_age":quote_age,"m5_age":m5_age,"heartbeat_age":heartbeat_age,"quote":quote,"contract":contract,"last_m5_timestamp":last_bar,"runner_status":runtime,"runner_pid":lock.get("pid") if lock else heartbeat.get("pid"),"kill_switch":STOP.exists(),"today":today,"signals":signals,"waiting":waiting,"positions":positions,"filled_count":filled_count,"closed":closed,"cumulative_r":sum(float(x.get("r_theoretical",0)) for x in closed),"drawdown_r":drawdown(signals),"next_checkpoint":next_checkpoint,"warnings":warnings,"gap":s.get("last_gap"),"stale_messages":if_stale,"events":event_rows(5)}
def render_watch(snapshot):
    q=snapshot["quote"]; c=snapshot["contract"]; d=snapshot["today"]; lines=["\033[2J\033[HLIQUIDITY_DISPLACEMENT_SCALP_V1 — FORWARD PAPER","="*64,"","STATUS",f"Bridge:             {snapshot['bridge']}",f"MT5 data:            {snapshot['mt5_state']}",f"Strategy hash:       {snapshot['hash'][:12]}...",f"Last update:         {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",f"Last M5 candle:      {datetime.fromtimestamp(snapshot['last_m5_timestamp'],timezone.utc).isoformat() if snapshot['last_m5_timestamp'] else 'N/A'}",f"Runner:              {snapshot['runner_status']}",f"Kill switch:         {'ON' if snapshot['kill_switch'] else 'OFF'}",f"Last quote:          {age_text(snapshot['quote_age'])}",f"Last runner heartbeat:{age_text(snapshot['heartbeat_age'])}","","TODAY",f"Setups:              {d['setups_detected']}",f"Waiting retrace:     {len(snapshot['waiting'])}",f"Filled:              {snapshot['filled_count']}",f"Expired/unfilled:    {d['expired_unfilled']}",f"Invalidated:         {d['invalidated']}",f"Fill rate:           {d['fill_rate_pct']:.1f}%","","MARKET",f"{SYMBOL} bid:         {q.get('bid','N/A')}",f"{SYMBOL} ask:         {q.get('ask','N/A')}",f"Spread:              {(float(q['ask'])-float(q['bid'])) if q.get('ask') is not None and q.get('bid') is not None else 'N/A'}",f"Spread points:       {q.get('spread_points',c.get('spread_points','N/A'))}",f"Current session:     {session(time.time())}","","PAPER POSITIONS",f"Open:                {len(snapshot['positions'])}"]
    for p in snapshot["positions"]: lines += [f"{p.get('direction')} entry {p.get('entry_realistic',p.get('entry_theoretical'))} stop {p.get('stop_loss')} target {p.get('target_theoretical')} state {p.get('status')}" ]
    lines += ["","WAITING SIGNALS"]
    if snapshot["waiting"]:
        for w in snapshot["waiting"]: lines.append(f"{w.get('direction')} — {w.get('status')} | entry {w.get('entry_theoretical')} stop {w.get('stop_loss')} target {w.get('target_theoretical')} | current {q.get('bid','N/A')}")
    else: lines.append("None")
    wins=sum(x.get("outcome")=="WIN" for x in snapshot["closed"]); losses=sum(x.get("outcome")=="LOSS" for x in snapshot["closed"]); rs=[float(x.get("r_theoretical",0)) for x in snapshot["closed"]]; gp=sum(x for x in rs if x>0); gl=sum(x for x in rs if x<0)
    lines += ["","FORWARD RESULTS",f"Filled trades:       {snapshot['filled_count']}",f"Wins:                {wins}",f"Losses:              {losses}",f"Time exits:          {sum(x.get('outcome')=='TIME_EXIT' for x in snapshot['closed'])}",f"Cumulative R:        {snapshot['cumulative_r']:.2f}",f"Expectancy:           {(sum(rs)/len(rs)) if rs else 'N/A'}",f"Profit factor:        {(gp/abs(gl)) if gl else 'N/A'}",f"Current DD:           {snapshot['drawdown_r']:.2f}R",f"Max DD:               {d.get('max_drawdown_r',0):.2f}R","","HISTORICAL REFERENCE","Fill rate:            42.4%","Validation expectancy:+0.222R","Validation PF:        1.473","Validation max DD:    4.04R","","EXECUTION",f"Median spread today:  {d.get('median_spread_price',0):.3f}",f"Median stop:          {d.get('median_stop_distance',0):.3f}",f"Median 0.01 risk:     {d.get('median_risk_at_001',0):.2f}",f"Forward fill rate:    {d.get('fill_rate_pct',0):.1f}%","Historical fill rate: 42.4%","","DRIFT WARNINGS"]
    lines += [x.get("code",str(x)) for x in snapshot["warnings"]] or ["None"]
    lines += ["","GAP MONITOR"]
    if snapshot.get("gap"):
        g=snapshot["gap"]; lines += ["WARNING: DATA GAP DETECTED",f"Last live candle: {g.get('last_live_candle')}",f"Current candle:    {g.get('current_candle')}",f"Missing M5 candles:{g.get('missing_m5_candles')}",f"Recovered:          {'YES' if g.get('recovery_status')=='COMPLETE' else 'NO / '+str(g.get('recovery_status','PENDING'))}","Recovery source:    historical completed candles"]
    else: lines.append("None")
    lines += ["","NEXT CHECKPOINT",f"{snapshot['next_checkpoint'] or 'Complete'} fills",f"Progress: {snapshot['filled_count']} / {snapshot['next_checkpoint'] or snapshot['filled_count']}","","RECENT EVENTS"]
    lines += [f"{event_clock(x.get('timestamp'))} {x.get('event','')} {x.get('direction',x.get('setup_id',''))}" for x in snapshot["events"]] or ["None"]
    lines += ["","Ctrl+C: Watch stopped. Forward paper runner remains active."]
    return "\n".join(lines)
def run_watch(interval):
    print("Starting read-only watch; it will not process strategy signals or mutate forward state.")
    try:
        while True:
            s=load_state(); data=None
            try:data=read_once()
            except Exception: data=None
            print(render_watch(watch_snapshot(s,data)))
            time.sleep(max(1,interval))
    except KeyboardInterrupt:
        print("\nWatch stopped. Forward paper runner remains active.")
def detect(s,data,source="LIVE_FORWARD"):
    m5,m15,h1,q,c=signal_features(data); out=[]; strategy=LiquidityDisplacementStrategy(CFG)
    if not m5:return out
    # Re-evaluate only recent completed sweep candidates; all inputs are completed.
    for i in range(max(40,len(m5)-18),len(m5)-1):
        ts=int(m5[i]["time"])+300
        telemetry=decision_telemetry(m5,m15,i,q,c)
        if telemetry and telemetry["candidate_id"] not in s.setdefault("decision_telemetry",{}):
            s["decision_telemetry"][telemetry["candidate_id"]]=telemetry
            event(s,"DECISION_TELEMETRY",{"candidate_id":telemetry["candidate_id"],"telemetry":telemetry,"source":source})
        cand=strategy.evaluate(m15,m5,q,c,datetime.fromtimestamp(ts,timezone.utc).isoformat(),i)
        if not cand:continue
        fill_i=cand.get("fill_index"); fill_key=int(m5[fill_i]["time"]) if fill_i is not None else "UNFILLED"; eid=f"LDSV1-{m5[i]['time']}-{fill_key}-{cand['direction']}"
        if eid in s["signals"]:continue
        sweep_time=datetime.fromtimestamp(int(m5[i]["time"])+300,timezone.utc).isoformat(); disp_time=datetime.fromtimestamp(int(m5[cand['displacement_index']]["time"])+300,timezone.utc).isoformat(); fill_time=datetime.fromtimestamp(int(m5[fill_i]["time"])+300,timezone.utc).isoformat() if fill_i is not None else None
        status="WAITING_FOR_RETRACE" if cand["status"]=="UNFILLED" else "FILLED"
        entry_theoretical=float(cand["entry"]); long=cand["direction"]=="LONG"
        # The strategy evaluator has already proven the retracement on completed
        # candle data. Do not compare that historical fill to a later live quote:
        # doing so rejects valid retracements after price has moved away.
        fill_bar=m5[fill_i] if fill_i is not None else None
        fill_points,fill_spread=price_spread_points({"spread_points":fill_bar.get("spread",q.get("spread_points",0))} if fill_bar else q,c)
        realistic_fill=(entry_theoretical+fill_spread/2) if status=="FILLED" and long else (entry_theoretical-fill_spread/2) if status=="FILLED" else None
        spread_points,spread_price=price_spread_points(q,c); target=entry_theoretical+(entry_theoretical-float(cand["stop_loss"]))*CFG.target_r if long else entry_theoretical-(float(cand["stop_loss"])-entry_theoretical)*CFG.target_r
        rec={"setup_id":eid,"version":"LIQUIDITY_DISPLACEMENT_SCALP_V1","source":source,"classification":"RECOVERED_FORWARD" if source=="GAP_RECOVERY" else "LIVE_OBSERVED","direction":cand["direction"],"status":status,"sweep_timestamp":sweep_time,"reclaim_timestamp":sweep_time,"displacement_timestamp":disp_time,"structure_break_timestamp":disp_time,"retracement_offered_timestamp":fill_time,"simulated_fill_timestamp":fill_time if status=="FILLED" else None,"simulated_close_timestamp":None,"entry_theoretical":entry_theoretical,"entry_realistic":realistic_fill,"entry_fill_difference":(realistic_fill-entry_theoretical) if realistic_fill is not None else None,"fill_confirmation":"COMPLETED_CANDLE_RETRACEMENT" if status=="FILLED" else None,"fill_candle_number":(int(fill_i)-int(cand["displacement_index"])) if fill_i is not None else None,"fill_candle_timestamp":fill_time,"stop_loss":cand["stop_loss"],"target_theoretical":cand["signal"].take_profit if cand.get("signal") else target,"spread_at_detection":cand["spread"],"spread_at_detection_points":spread_points,"spread_at_detection_price":spread_price,"spread_at_displacement_points":spread_points,"spread_at_displacement_price":spread_price,"spread_at_retracement_points":fill_points if fill_bar else spread_points,"spread_at_retracement_price":fill_spread if fill_bar else spread_price,"spread_at_entry_points":fill_points if realistic_fill is not None else None,"spread_at_entry_price":fill_spread if realistic_fill is not None else None,"spread_at_exit_points":None,"spread_at_exit_price":None,"stop_distance":cand["risk"],"spread_stop_ratio":cand["spread"]/cand["risk"] if cand["risk"] else 0,"risk_at_001":(cand["risk"]/c["tick_size"])*c["tick_value"]*c["min_lot"],"required_balance_0_5":((cand["risk"]/c["tick_size"])*c["tick_value"]*c["min_lot"])/.005,"required_balance_1_0":((cand["risk"]/c["tick_size"])*c["tick_value"]*c["min_lot"])/.01,"required_balance_2_0":((cand["risk"]/c["tick_size"])*c["tick_value"]*c["min_lot"])/.02,"fill_index":fill_i,"bar_index":i,"displacement_index":cand["displacement_index"],"detected_at":now(),"slippage_assumption_price":SLIPPAGE,"outcome":None,"r_theoretical":None,"r_realistic":None,"duration_minutes":None,"mae_price":None,"mfe_price":None,"mae_r":None,"mfe_r":None,"exit_reason":None,"quote_at_detection":q,"contract_at_detection":c}
        s["signals"][eid]=rec; event(s,"DETECTED",{"setup_id":eid,"direction":cand["direction"],"source":source}); event(s,status,{"setup_id":eid,"direction":cand["direction"],"source":source})
    return out
def process(s,data,source="LIVE_FORWARD"):
    m5,m15,h1,q,c=signal_features(data); last=int(m5[-1]["time"]) if m5 else None
    if last is None:return
    s["last_mt5_data_timestamp"]=data.get("timestamp"); s["last_poll_timestamp"]=now()
    if last==s.get("last_candle"):
        save(s); return
    detect(s,data,source)
    # Expire/waiting lifecycle; a candidate is marked filled only if the live quote
    # confirms side-specific retracement at the offered level.
    for rec in s["signals"].values():
        if rec["status"]=="WAITING_FOR_RETRACE":
            # Strategy detector emits WAITING only after the fixed three-candle window.
            rec["status"]="UNFILLED_EXPIRED"; rec["exit_reason"]="UNFILLED_EXPIRED"; event(s,"UNFILLED_EXPIRED",{"setup_id":rec["setup_id"],"exit_reason":rec["exit_reason"],"source":source})
        follow_unfilled(rec,m5)
        settle_filled_record(s,rec,m5,q,c,source)
    s["last_candle"]=last; s["gold_symbols"]=data.get("gold_symbols",[])
    filled_count=len([x for x in s["signals"].values() if x.get("outcome")])
    for threshold in (25,50,100):
        if filled_count>=threshold and threshold not in s.get("checkpoint_thresholds",[]):
            s.setdefault("checkpoint_thresholds",[]).append(threshold); checkpoint(s,threshold)
    save(s)
def checkpoint(s,threshold=None):
    filled=[x for x in s["signals"].values() if x.get("outcome")]; rs=[x.get("r_theoretical",0) for x in filled]; rr=[x.get("r_realistic",0) for x in filled]; wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]; eq=peak=dd=0; streak=longest=0
    for r in rs:eq+=r;peak=max(peak,eq);dd=max(dd,peak-eq);streak=streak+1 if r<0 else 0;longest=max(longest,streak)
    durations=[(datetime.fromisoformat(x["simulated_close_timestamp"]).timestamp()-datetime.fromisoformat(x["simulated_fill_timestamp"]).timestamp())/60 for x in filled if x.get("simulated_close_timestamp") and x.get("simulated_fill_timestamp")]
    live=[x for x in s["signals"].values() if x.get("classification","LIVE_OBSERVED")=="LIVE_OBSERVED"]; recovered=[x for x in s["signals"].values() if x.get("classification")=="RECOVERED_FORWARD"]
    out={"timestamp":now(),"threshold":threshold,"filled":len(filled),"win_rate_pct":100*len(wins)/(len(wins)+len(losses)) if wins or losses else 0,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"expectancy_r":sum(rs)/len(rs) if rs else 0,"cumulative_r":sum(rs),"max_drawdown_r":dd,"longest_losing_streak":longest,"average_duration_minutes":sum(durations)/len(durations) if durations else 0,"unfilled_expired":sum(x.get("status")=="UNFILLED_EXPIRED" for x in s["signals"].values()),"fill_rate_pct":100*len(filled)/len(s["signals"]) if s["signals"] else 0,"realistic_expectancy_r":sum(rr)/len(rr) if rr else 0,"realistic_cumulative_r":sum(rr),"mean_spread":sum(float(x.get("spread_at_detection",0)) for x in filled)/len(filled) if filled else 0,"live_observed":metric_block(live),"recovered":metric_block(recovered),"combined_research":metric_block(live+recovered),"historical_validation":{"expectancy_r":0.222,"profit_factor":1.473,"max_drawdown_r":4.04},"degradation":{"expectancy_r":(sum(rs)/len(rs) if rs else 0)-0.222,"max_drawdown_multiple":dd/4.04 if dd else 0}}
    s["checkpoints"].append(out); save(s); return out
def write_summary(s):
    m=manifest(); today=datetime.now(timezone.utc).date().isoformat(); rows=["# LIQUIDITY_DISPLACEMENT_SCALP_V1 forward paper testing","","READ-ONLY / PAPER-ONLY. No order submission endpoints are used.","",f"Version source SHA256: `{m['source_sha256']}`",f"Started: `{s.get('started_at')}`",f"Signals tracked: {len(s.get('signals',{}))}",f"Filled records: {len(s.get('fills',[]))}","","## Frozen configuration",json.dumps(m,indent=2),"","## Checkpoint commands","`python3 liquidity_displacement_forward.py start --interval 15`","`python3 liquidity_displacement_forward.py status`","`python3 liquidity_displacement_forward.py checkpoint`","`python3 liquidity_displacement_forward.py daily`","`python3 liquidity_displacement_forward.py health`","`python3 liquidity_displacement_forward.py stop`","","## Latest checkpoints",json.dumps(s.get("checkpoints",[])[-10:],indent=2),"","## DAILY FORWARD HEALTH",json.dumps(daily_aggregate(s,today),indent=2),"","## Symbol research",json.dumps(s.get("gold_symbols",[]),indent=2)]
    SUMMARY.write_text("\n".join(rows))

def decision_telemetry(m5, m15, i, quote, contract):
    """Audit a meaningful sweep candidate without participating in execution."""
    if i < 40 or i >= len(m5):
        return None
    strategy=LiquidityDisplacementStrategy(CFG)
    low_level,high_level,_,_=strategy._levels(m5,i)
    if low_level is None or high_level is None:
        return None
    bar=m5[i]; low=_candle(bar,"low"); high=_candle(bar,"high"); close=_candle(bar,"close")
    long_breach=low<low_level; short_breach=high>high_level
    if not long_breach and not short_breach:
        return None
    direction="LONG" if long_breach and close>low_level else "SHORT" if short_breach and close<high_level else "LONG" if long_breach else "SHORT"
    sweep_level=low_level if direction=="LONG" else high_level; sweep_price=low if direction=="LONG" else high
    candidate_id=f"CAND-{CFG.symbol}-{int(bar['time'])}-{direction}"
    out={"candidate_id":candidate_id,"symbol":CFG.symbol,"timestamp":datetime.fromtimestamp(int(bar["time"])+300,timezone.utc).isoformat(),"direction":direction,"sweep":{"passed":True,"level":sweep_level,"sweep_price":sweep_price,"distance":abs(sweep_price-sweep_level),"breached":True},"reclaim":{"passed":(close>low_level if direction=="LONG" else close<high_level),"required_level":sweep_level,"close_price":close},"displacement":{"passed":False,"candle_range":None,"body":None,"atr":None,"threshold_body_atr":None,"threshold_median_body":None,"observed_body_atr":None,"observed_body_multiple":None,"candle_timestamp":None},"mss":{"passed":False,"structure_level":None,"break_price":None,"break_distance":None},"entry":{"armed":False,"entry_fraction":ENTRY_FRACTION,"entry_price":None,"candles_waited":None,"filled":False},"final_status":None,"reason_code":None}
    if not out["reclaim"]["passed"]:
        out["final_status"]="REJECTED_NO_RECLAIM"; out["reason_code"]="REJECTED_NO_RECLAIM"; return out
    a=atr(m5[max(0,i-119):i+1])[-1]; prior=m5[max(0,i-12):i]; bodies=[abs(_candle(x,"close")-_candle(x,"open")) for x in prior]; median_body=sorted(bodies)[len(bodies)//2] if bodies else 0
    displacement_i=None; mss_i=None; best=None
    for j in range(i+1,min(len(m5),i+1+CFG.max_structure_break_candles)):
        d=m5[j]; rng=_candle(d,"high")-_candle(d,"low"); body=abs(_candle(d,"close")-_candle(d,"open")); close_loc=(_candle(d,"close")-_candle(d,"low"))/rng if rng>0 else .5; micro=max(_candle(x,"high") for x in m5[max(i-5,i-12):i]) if direction=="LONG" else min(_candle(x,"low") for x in m5[max(i-5,i-12):i]); directional=(direction=="LONG" and _candle(d,"close")>_candle(d,"open") and close_loc>=CFG.min_close_location) or (direction=="SHORT" and _candle(d,"close")<_candle(d,"open") and close_loc<=1-CFG.min_close_location); strong=body>=a*CFG.min_body_atr and body>=median_body*CFG.min_body_median_multiple; best=best if best and best["body"]>=body else {"body":body,"range":rng,"atr":a,"median_body":median_body,"timestamp":int(d["time"]),"micro":micro};
        if strong and directional:
            displacement_i=j; out["displacement"]={"passed":True,"candle_range":rng,"body":body,"atr":a,"threshold_body_atr":a*CFG.min_body_atr,"threshold_median_body":median_body*CFG.min_body_median_multiple,"observed_body_atr":body/a if a else None,"observed_body_multiple":body/median_body if median_body else None,"candle_timestamp":datetime.fromtimestamp(int(d["time"])+300,timezone.utc).isoformat()}; shifted=_candle(d,"close")>micro if direction=="LONG" else _candle(d,"close")<micro
            if shifted: mss_i=j; out["mss"]={"passed":True,"structure_level":micro,"break_price":_candle(d,"close"),"break_distance":abs(_candle(d,"close")-micro)}; break
    if displacement_i is None:
        if best: out["displacement"].update({"candle_range":best["range"],"body":best["body"],"atr":best["atr"],"threshold_body_atr":best["atr"]*CFG.min_body_atr,"threshold_median_body":best["median_body"]*CFG.min_body_median_multiple,"observed_body_atr":best["body"]/best["atr"] if best["atr"] else None,"observed_body_multiple":best["body"]/best["median_body"] if best["median_body"] else None,"candle_timestamp":datetime.fromtimestamp(best["timestamp"]+300,timezone.utc).isoformat()})
        out["final_status"]="REJECTED_DISPLACEMENT_TOO_WEAK"; out["reason_code"]="REJECTED_DISPLACEMENT_TOO_WEAK"; return out
    if mss_i is None:
        out["final_status"]="REJECTED_NO_MSS"; out["reason_code"]="REJECTED_NO_MSS"; return out
    d=m5[mss_i]; rng=_candle(d,"high")-_candle(d,"low"); entry=_candle(d,"high")-rng*ENTRY_FRACTION if direction=="LONG" else _candle(d,"low")+rng*ENTRY_FRACTION; out["entry"].update({"armed":True,"entry_price":entry})
    for j in range(mss_i+1,min(len(m5),mss_i+1+CFG.max_retrace_candles)):
        c=m5[j]; touched=_candle(c,"low")<=entry if direction=="LONG" else _candle(c,"high")>=entry; held=_candle(c,"close")>=entry if direction=="LONG" else _candle(c,"close")<=entry
        if touched and held:
            out["entry"].update({"candles_waited":j-mss_i,"filled":True}); out["final_status"]="FILLED"; out["reason_code"]="FILLED"; return out
    out["entry"]["candles_waited"]=CFG.max_retrace_candles; out["final_status"]="ENTRY_EXPIRED_NO_RETRACE"; out["reason_code"]="ENTRY_EXPIRED_NO_RETRACE"; return out

HISTORICAL_REFERENCE={"fill_rate_pct":42.4,"expectancy_R":0.222,"profit_factor":1.473,"max_drawdown_R":4.04,"provenance":"hardcoded constant in liquidity_displacement_forward.py; no backtest artifact reference"}

def _age_seconds(iso_ts,now_dt):
    if not iso_ts:return None
    try:
        ts=datetime.fromisoformat(str(iso_ts).replace("Z","+00:00")) if isinstance(iso_ts,str) else datetime.fromtimestamp(float(iso_ts),timezone.utc)
        return (now_dt-ts).total_seconds()
    except (TypeError,ValueError):return None

def _standard_position(r,symbol):
    return {"economic_position_id":r.get("setup_id"),"setup_id":r.get("setup_id"),"symbol":symbol,"direction":r.get("direction"),"entry_time":r.get("simulated_fill_timestamp"),"entry_price":r.get("entry_realistic",r.get("entry_theoretical")),"status":r.get("status")}

def _standard_closed_position(r,symbol):
    row=_standard_position(r,symbol)
    row.update({"close_time":r.get("simulated_close_timestamp"),"close_price":r.get("exit_price_realistic",r.get("exit_price_theoretical")),"outcome":r.get("outcome"),"realized_R":r.get("r_theoretical"),"mfe_R":r.get("mfe_r"),"mae_R":r.get("mae_r"),"exit_reason":r.get("exit_reason")})
    return row

def build_standard_report(s,instance,daily_records=None,event_records=None):
    """Standard cross-strategy observability report for one liquidity
    displacement instance. Takes state and instance identity as explicit
    parameters and never reads module globals for identity/path purposes —
    this is what lets the Control API call it for any instance's state
    without configure()/module mutation. No writes.
    """
    now_dt=datetime.now(timezone.utc)
    today=now_dt.date().isoformat()
    h=health(s)
    watch=watch_snapshot(s,data=None,now_ts=time.time())
    daily=daily_aggregate(s,today,daily_records=daily_records)
    records=list(s.get("signals",{}).values())
    fills=trade_records(s)
    summary=trade_summary(fills,records)
    closed=[r for r in fills if r.get("outcome")]
    open_positions=[r for r in fills if r.get("status")=="FILLED" and not r.get("outcome")]
    heartbeat_ts=h["heartbeat"].get("timestamp") if isinstance(h.get("heartbeat"),dict) else None
    checkpoints=s.get("checkpoints",[])
    last_checkpoint=checkpoints[-1] if checkpoints else None
    max_dd=drawdown(records)

    gap=s.get("last_gap")
    if not gap:
        gap_status="HEALTHY"; recovered=None; recovery_source=None
    elif gap.get("recovery_status") in ("COMPLETE","PARTIAL"):
        gap_status="RECOVERED_GAP"; recovered=gap.get("recovery_status")=="COMPLETE"; recovery_source="GAP_RECOVERY replay from completed M5 candles"
    else:
        gap_status="GAP_DETECTED"; recovered=False; recovery_source=None

    decision_telemetry=s.get("decision_telemetry",{})
    reason_counts={}
    for row in decision_telemetry.values():
        code=row.get("reason_code") or "UNKNOWN"
        reason_counts[code]=reason_counts.get(code,0)+1
    recent_candidates=sorted(decision_telemetry.values(),key=lambda r:r.get("timestamp") or "")[-10:]

    reference_performance=dict(HISTORICAL_REFERENCE)
    reference_performance["reference_label"]="Historical Reference"

    return {
        "identity":{
            "strategy_id":instance["instance_id"],
            "display_name":instance.get("label",instance["instance_id"]),
            "strategy_version":s.get("version",{}).get("version"),
            "configuration_version":None,
            "source_identity":s.get("version",{}).get("source_sha256"),
            "decision_fingerprint":None,
            "freeze_identity":None,
            "freeze_timestamp":None,
            "observability_version":"liquidity-report-1",
            "sample_boundary":s.get("started_at"),
            "observed_at":now_dt.isoformat(),
        },
        "status":{
            "runner_status":h["runner_status"],
            "last_runner_heartbeat":heartbeat_ts,
            "runner_heartbeat_age":_age_seconds(heartbeat_ts,now_dt),
            "bridge_status":watch.get("bridge"),
            "data_status":watch.get("mt5_state"),
            "last_market_timestamp":s.get("last_mt5_data_timestamp"),
            "data_age":_age_seconds(s.get("last_poll_timestamp"),now_dt),
            "kill_switch":instance["stop_path"].exists(),
            "observability_timestamp":now_dt.isoformat(),
        },
        "sample":{"scope":"FORWARD_PAPER","boundary":s.get("started_at")},
        "lifecycle":[
            {"stage":"detected","label":"Setups Detected","count":summary["setups_detected"]},
            {"stage":"waiting","label":"Waiting For Retrace","count":summary["waiting_retrace"]},
            {"stage":"entered","label":"Filled","count":summary["total_fills"]},
            {"stage":"open","label":"Open","count":summary["open_paper_positions"]},
            {"stage":"closed","label":"Closed","count":len(closed)},
            {"stage":"expired","label":"Expired Unfilled","count":summary["expired_unfilled"]},
        ],
        "performance":{
            "trades":len(closed),
            "wins":summary["wins"],
            "losses":summary["losses"],
            "breakevens":0,
            "other":summary["time_exits"],
            "win_rate":summary["win_rate_pct"],
            "realized_R":summary["cumulative_r"] if closed else None,
            "expectancy_R":summary["expectancy_r"],
            "profit_factor":summary["profit_factor"],
            "max_drawdown_R":max_dd if closed else None,
            "open":summary["open_paper_positions"],
            # current_drawdown_R omitted: this strategy tracks a single
            # peak-to-trough drawdown metric (drawdown()), not a distinct
            # current-vs-max pair — reporting a fabricated "current" figure
            # would misrepresent what the source actually computes.
        },
        "symbols":[{"symbol":instance["symbol"],"detected":summary["setups_detected"],"opportunities":summary["total_fills"],"entries":summary["total_fills"],"open":summary["open_paper_positions"],"closed":len(closed),"realized_R":summary["cumulative_r"] if closed else None}],
        "open_positions":[_standard_position(r,instance["symbol"]) for r in open_positions],
        "closed_positions":[_standard_closed_position(r,instance["symbol"]) for r in closed],
        "rejection_reasons":[
            {"reason_code":"UNFILLED_EXPIRED","display_reason":"Retrace Opportunity Expired","count":summary["expired_unfilled"]},
            {"reason_code":"INVALIDATED","display_reason":"Setup Invalidated","count":sum(1 for r in records if r.get("status")=="INVALIDATED")},
        ],
        "data_quality":{
            "data_status":watch.get("mt5_state"),
            "last_market_timestamp":s.get("last_mt5_data_timestamp"),
            "freshness":_age_seconds(s.get("last_poll_timestamp"),now_dt),
            "gap_status":gap_status,
            "missing_observations":gap.get("missing_m5_candles") if gap else None,
            "recovered":recovered,
            "recovery_source":recovery_source,
        },
        "recent_activity":[
            {
                "timestamp":row.get("timestamp"),
                "strategy_id":instance["instance_id"],
                "symbol":instance["symbol"],
                "event_type":row.get("event"),
                "display_event":str(row.get("event","")).replace("_"," ").title() or None,
                "direction":None,
                "setup_id":row.get("setup_id"),
                "opportunity_id":row.get("setup_id"),
                "economic_position_id":row.get("setup_id"),
                "reason_code":row.get("exit_reason"),
                "display_reason":str(row.get("exit_reason") or "").replace("_"," ").title() or None,
                "metadata":row,
            }
            for row in [r for r in (event_records if event_records is not None else event_rows(200)) if r.get("event") not in ("DECISION_TELEMETRY", "READ_ERROR")][-10:]
        ],
        "reference_performance":reference_performance,
        "extensions":{
            "strategy_id":"LIQUIDITY_DISPLACEMENT_SCALP_V1",
            "drift_warnings":daily.get("warnings",[]),
            "decision_telemetry_summary":{"total_candidates":len(decision_telemetry),"by_reason_code":reason_counts,"recent_candidates":recent_candidates},
            "account_feasibility":daily.get("account_feasibility"),
            "checkpoint_progress":{
                "filled_count":watch.get("filled_count"),
                "next_threshold":watch.get("next_checkpoint"),
                "last_checkpoint":last_checkpoint,
            },
        },
    }

def finalize_runner(s,interval):
    s["running"]=False; save(s); write_heartbeat(s,"STOPPED",interval,now()); release_lock(); write_summary(s)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["start","status","stop","checkpoint","daily","health","watch","trades","report"]); ap.add_argument("--interval",type=int,default=15); ap.add_argument("--date",default=None); ap.add_argument("--limit",type=int,default=None); a=ap.parse_args(); s=load_state()
    if s.get("source_sha256_at_start") and s["source_sha256_at_start"]!=source_hash(): raise SystemExit("strategy source hash changed; refusing to run")
    if a.command=="status": print(json.dumps({"running":s.get("running"),"runner_status":runtime_status(s)[0],"version":s["version"],"signals":len(s.get("signals",{})),"last_candle":s.get("last_candle"),"heartbeat":read_json(HEARTBEAT,{}),"latest_checkpoint":s.get("checkpoints",[])[-1:]},indent=2)); return
    if a.command=="health": print(json.dumps(health(s),indent=2)); return
    if a.command=="report":
        instance={"instance_id":manifest()["version"],"family_id":"LIQUIDITY_DISPLACEMENT_SCALP_V1","symbol":SYMBOL,"label":manifest()["version"],"stop_path":STOP}
        print(format_standard_report(build_standard_report(s,instance))); return
    if a.command=="watch": run_watch(a.interval); return
    if a.command=="trades": print(render_trades(s,a.date,a.limit)); return
    if a.command=="daily":
        date_text=a.date or datetime.now(timezone.utc).date().isoformat(); result=daily_aggregate(s,date_text); persisted=persist_daily(result) if date_text < datetime.now(timezone.utc).date().isoformat() else False; result["persisted_new_record"]=persisted; print(json.dumps(result,indent=2)); return
    if a.command=="stop": STOP.touch(); s["running"]=False; save(s); write_summary(s); print("paper runner stopped; kill switch created"); return
    if a.command=="checkpoint": print(json.dumps(checkpoint(s),indent=2)); return
    if not acquire_lock(s):return
    if STOP.exists(): STOP.unlink()
    s["running"]=True; s["started_at"]=s.get("started_at") or now(); s["source_sha256_at_start"]=source_hash(); s["version"]=manifest();
    if not MANIFEST.exists(): MANIFEST.write_text(json.dumps(s["version"],indent=2))
    save(s); write_heartbeat(s,"RUNNING",a.interval); event(s,"FORWARD_STARTED",{"version":s["version"],"source":"LIVE_FORWARD"})
    stopping=False
    def request_stop(signum,frame):
        nonlocal stopping
        stopping=True
    old_int=signal.signal(signal.SIGINT,request_stop); old_term=signal.signal(signal.SIGTERM,request_stop)
    try:
        while s["running"] and not STOP.exists() and not stopping:
            s["last_poll_timestamp"]=now(); write_heartbeat(s,"RUNNING",a.interval)
            try:
                data=read_once(); s=load_state(); s["last_poll_timestamp"]=now(); s["last_successful_mt5_read"]=now(); s["last_read_error"]=None
                gap=detect_gap(s,data)
                if gap:
                    s["last_gap"]=gap
                    gap_signature=f"{gap['gap_start']}|{gap['gap_end']}"
                    if s.get("last_gap_signature")!=gap_signature:
                        s["last_gap_signature"]=gap_signature; event(s,"DATA_GAP_DETECTED",{"gap":gap,"source":"GAP_RECOVERY"})
                    replay_gap(s,data,gap)
                process(s,data,"LIVE_FORWARD"); write_summary(s)
            except Exception as e:
                s["last_read_error"]=str(e); event(s,"READ_ERROR",{"error":str(e),"source":"LIVE_FORWARD"}); save(s)
            write_heartbeat(s,"RUNNING",a.interval); time.sleep(max(5,a.interval)); s=load_state()
    finally:
        signal.signal(signal.SIGINT,old_int); signal.signal(signal.SIGTERM,old_term); finalize_runner(load_state(),a.interval)
if __name__=="__main__":main()
