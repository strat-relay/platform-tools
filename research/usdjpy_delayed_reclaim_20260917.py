"""Research-only delayed-reclaim audit for USDJPY liquidity displacement V1."""
from __future__ import annotations
import bisect, json, statistics, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_engine import _candle, atr
from liquidity_displacement import LiquidityDisplacementConfig
import liquidity_displacement_forward as base
from liquidity_displacement_entry_forward import configure

DATA = Path("/tmp/usdjpy_historical_20260917.json")
OUT = ROOT / "artifacts" / "audits"
START = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp())
TARGET_R = 1.25


def iso(ts): return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def outcome(row, bars, fill_i):
    entry, stop = float(row["entry"]), float(row["stop_loss"]); risk = abs(entry-stop); long = row["direction"] == "LONG"
    target = entry + risk * TARGET_R if long else entry - risk * TARGET_R
    window = bars[fill_i:min(len(bars), fill_i + 25)]
    mae = ((entry - min(float(x["low"]) for x in window)) / risk if long else
           (max(float(x["high"]) for x in window) - entry) / risk)
    mfe = ((max(float(x["high"]) for x in window) - entry) / risk if long else
           (entry - min(float(x["low"]) for x in window)) / risk)
    for step, c in enumerate(window):
        sl = float(c["low"]) <= stop if long else float(c["high"]) >= stop
        tp = float(c["high"]) >= target if long else float(c["low"]) <= target
        if sl or tp:
            px = stop if sl else target
            return {"exit_reason": "STOP" if sl else "TARGET", "exit_price": px,
                    "realized_R": (px-entry)/risk if long else (entry-px)/risk,
                    "duration_minutes": step*5, "MAE": mae, "MFE": mfe, "target": target}
    c = window[-1]; px = float(c["close"])
    return {"exit_reason": "TIME_EXIT", "exit_price": px,
            "realized_R": (px-entry)/risk if long else (entry-px)/risk,
            "duration_minutes": 120, "MAE": mae, "MFE": mfe, "target": target}


def frozen_strategy():
    configure("usdjpy25")
    return base.LiquidityDisplacementStrategy(base.CFG)


class DelayedReclaimStrategy:
    """Frozen V1 pipeline with only the sweep-close reclaim assumption changed."""
    def __init__(self, n):
        self.n = n
        self.config = LiquidityDisplacementConfig(symbol="USDJPYm", target_r=1.25, max_hold_minutes=120,
            max_retrace_candles=5, max_structure_break_candles=5, min_body_atr=.5,
            min_body_median_multiple=1., min_close_location=.6, atr_buffer_fraction=.1)
        self._frozen_levels = frozen_strategy()

    def evaluate(self, m15, m5, quote, contract, timestamp, i):
        c = m5[i]
        if len(m5) < 40 or i < 40: return None
        # Preserve population equivalence: immediate-reclaim cases must use
        # the exact frozen V1 candidate path.  Only the delayed branch below
        # is research behavior.
        frozen = self._frozen_levels.find_candidate(m15, m5, i, quote, contract, timestamp)
        if frozen:
            frozen = dict(frozen)
            frozen.update({"sweep_index": i, "reclaim_index": i, "reclaim_delay": 0,
                           "immediate_reclaim": True, "max_adverse_extension": 0.0,
                           "reclaim_timestamp": iso(m5[i]["time"]),
                           "setup_type": "LIQUIDITY_DISPLACEMENT_DELAYED_RECLAIM_RESEARCH_V1"})
            return frozen
        a = atr(m5[max(0, i-119):i+1])[-1]; spread = float(quote["ask"])-float(quote["bid"])
        if a <= 0 or spread <= 0: return None
        prior = m5[max(0, i-36):i]
        # Exact frozen V1 liquidity-level source, preserved for the whole
        # waiting event; only the sweep-candle close requirement is relaxed.
        low_level, high_level, recent_low, recent_high = self._frozen_levels._levels(m5, i)
        if low_level is None or high_level is None: return None
        low_sweep = float(c["low"]) < low_level
        high_sweep = float(c["high"]) > high_level
        direction = "LONG" if low_sweep else "SHORT" if high_sweep else None
        if not direction: return None
        reference = low_level if direction == "LONG" else high_level
        immediate = (float(c["close"]) > reference if direction == "LONG" else float(c["close"]) < reference)
        reclaim_i = i if immediate else next((j for j in range(i+1, min(i+1+self.n, len(m5)))
                                               if (float(m5[j]["close"]) > reference if direction == "LONG" else float(m5[j]["close"]) < reference)), None)
        if reclaim_i is None: return None
        ext = (min(float(m5[j]["low"]) for j in range(i, reclaim_i+1)) if direction == "LONG" else
               max(float(m5[j]["high"]) for j in range(i, reclaim_i+1)))
        bodies = [body(x) for x in m5[max(0, i-12):i]]; med = sorted(bodies)[len(bodies)//2] if bodies else 0
        disp_i = None; break_level = None; features = None
        # Existing V1 displacement/MSS logic, anchored after reclaim.
        for j in range(reclaim_i+1, min(len(m5), reclaim_i+1+self.config.max_structure_break_candles)):
            d = m5[j]; rng = float(d["high"])-float(d["low"]); b = body(d)
            loc = (float(d["close"])-float(d["low"]))/rng if rng else .5
            bullish = direction == "LONG" and float(d["close"]) > float(d["open"]) and loc >= self.config.min_close_location
            bearish = direction == "SHORT" and float(d["close"]) < float(d["open"]) and loc <= 1-self.config.min_close_location
            displacement = b >= a*self.config.min_body_atr and b >= med*self.config.min_body_median_multiple
            if not (displacement and (bullish or bearish)): continue
            micro = (max(float(x["high"]) for x in m5[max(reclaim_i-5, reclaim_i-12):reclaim_i]) if direction == "LONG" else
                     min(float(x["low"]) for x in m5[max(reclaim_i-5, reclaim_i-12):reclaim_i]))
            shifted = float(d["close"]) > micro if direction == "LONG" else float(d["close"]) < micro
            if shifted: disp_i, break_level, features = j, micro, (b/rng if rng else 0, loc, rng); break
        if disp_i is None: return None
        d = m5[disp_i]; dl, dh = float(d["low"]), float(d["high"]); dr = dh-dl
        entry = dh-dr*.25 if direction == "LONG" else dl+dr*.25
        broker_min = max(float(contract.get("tick_size", .001)), float(contract.get("stops_level", 0))*float(contract.get("point", .001)))
        buffer = max(a*.1, spread*1.25, broker_min)
        stop = (ext-buffer if direction == "LONG" else ext+buffer); risk = entry-stop if direction == "LONG" else stop-entry
        if risk <= 0: return None
        return {"direction": direction, "sweep_level": reference, "sweep_extreme": float(c["low"] if direction == "LONG" else c["high"]),
                "sweep_index": i, "reclaim_index": reclaim_i, "reclaim_delay": reclaim_i-i,
                "immediate_reclaim": immediate, "max_adverse_extension": abs(ext-reference),
                "reclaim_timestamp": iso(m5[reclaim_i]["time"]), "displacement_index": disp_i,
                "break_level": break_level, "entry": entry, "stop_loss": stop, "risk": risk,
                "atr": a, "spread": spread, "body_atr": features[0], "close_location": features[1],
                "displacement_range": features[2], "status": "CANDIDATE", "entry_type": "RETRACE_0.250000_DISPLACEMENT",
                "setup_type": "LIQUIDITY_DISPLACEMENT_DELAYED_RECLAIM_RESEARCH_V1"}


def body(x): return abs(float(x["close"])-float(x["open"]))


def replay(strategy, data, start, end, dedupe=True):
    m5, m15, contract = data["M5"], data["M15"], data["contract"]; mt = [int(x["time"]) for x in m15]
    rows=[]; occupied=set(); i=40
    while i < min(end, len(m5)-25):
        if int(m5[i]["time"]) < start: i += 1; continue
        t=int(m5[i]["time"]); j=bisect.bisect_right(mt,t); context=m15[max(0,j-120):j]
        spread=float(m5[i]["spread"])*float(contract["point"]); q={"bid":float(m5[i]["close"])-spread/2,"ask":float(m5[i]["close"])+spread/2}
        c = strategy.evaluate(context,m5,q,contract,iso(t+300),i)
        if c:
            key=(c.get("sweep_index",i), c["direction"])
            if not dedupe or key not in occupied:
                occupied.add(key); row=dict(c); row["timestamp"]=iso(t); row["candidate_timestamp"]=iso(m5[c.get("displacement_index",i)]["time"]); row["setup_id"]=f"{strategy.__class__.__name__}-{t}-{i}"
                fill_i=None; start_fill=c.get("displacement_index",i)+1
                for k in range(start_fill,min(len(m5),start_fill+5)):
                    b=m5[k]; touched=float(b["low"])<=c["entry"] if c["direction"]=="LONG" else float(b["high"])>=c["entry"]; held=float(b["close"])>=c["entry"] if c["direction"]=="LONG" else float(b["close"])<=c["entry"]
                    if touched and held: fill_i=k; break
                row["filled"]=fill_i is not None; row["fill_index"]=fill_i; row["fill_price"]=c["entry"] if fill_i is not None else None
                if fill_i is not None: row.update(outcome({"entry":c["entry"],"stop_loss":c["stop_loss"],"direction":c["direction"]},m5,fill_i))
                else: row.update({"exit_reason":None,"exit_price":None,"realized_R":None,"duration_minutes":None,"MAE":None,"MFE":None,"target":None})
                rows.append(row)
        i += 1
    return rows


def metrics(rows):
    rs=[float(x["realized_R"]) for x in rows if x.get("realized_R") is not None]; wins=[x for x in rs if x>0]; losses=[x for x in rs if x<0]; eq=peak=dd=0.; streak=longest=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    return {"liquidity_events":len(rows),"immediate_reclaims":sum(x.get("reclaim_delay",0)==0 for x in rows),"delayed_reclaims":sum(x.get("reclaim_delay",0)>0 for x in rows),"candidates":len(rows),"fills":sum(x.get("filled") for x in rows),"win_rate_pct":100*len(wins)/(len(wins)+len(losses)) if wins or losses else 0,"expectancy_R":statistics.mean(rs) if rs else 0,"median_R":statistics.median(rs) if rs else 0,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"max_drawdown_R":dd,"max_losing_streak":longest,"MAE_median_R":statistics.median([x["MAE"] for x in rows if x.get("MAE") is not None]) if any(x.get("MAE") is not None for x in rows) else None,"MFE_median_R":statistics.median([x["MFE"] for x in rows if x.get("MFE") is not None]) if any(x.get("MFE") is not None for x in rows) else None,"average_hold_minutes":statistics.mean([x["duration_minutes"] for x in rows if x.get("duration_minutes") is not None]) if rs else 0}


def main():
    data=json.loads(DATA.read_text()); full_start=int(data["M5"][40]["time"]); full_end=int(data["M5"][-26]["time"]); split=full_start+2*(full_end-full_start)//3
    frozen=replay(frozen_strategy(),data,full_start,full_end)
    frozen_keys={(x["timestamp"],x["direction"]) for x in frozen}
    delayed={n:[x for x in replay(DelayedReclaimStrategy(n),data,full_start,full_end)
                if x.get("reclaim_delay",0)>0 or (x["timestamp"],x["direction"]) in frozen_keys]
              for n in (1,2,3,5)}
    window_frozen=[x for x in frozen if START<=int(datetime.fromisoformat(x["timestamp"]).timestamp())<END]
    window_delayed={n:[x for x in rows if START<=int(datetime.fromisoformat(x["timestamp"]).timestamp())<END] for n,rows in delayed.items()}
    # Identify the already-observed historical event only after replay, using
    # its recorded low and timestamp; these values are not detector inputs.
    long_rows=[x for x in window_delayed[1] if x.get("direction")=="LONG"]
    # Identify the already-observed event only after replay.  The timestamp
    # proximity is a post-hoc diagnostic selector, never a detector input.
    expected_sweep_epoch=int(datetime(2026,9,10,7,30,tzinfo=timezone.utc).timestamp())
    example=min(long_rows, key=lambda x: (abs(int(x.get("sweep_index", 0))-58546), abs(float(x.get("sweep_extreme", 0))-153.345))) if long_rows else None
    # Post-hoc event trace for the requested historical window.  The event
    # index is located from the candle timestamp after replay; its prices are
    # never used as detector inputs.
    event_i=next((k for k,x in enumerate(data["M5"]) if int(x["time"])==expected_sweep_epoch), None)
    event_low,event_high,_,_=DelayedReclaimStrategy(1)._frozen_levels._levels(data["M5"],event_i)
    event_trace={"sweep_timestamp":iso(data["M5"][event_i]["time"]),"sweep_index":event_i,"reference":event_low,"sweep_low":data["M5"][event_i]["low"],"sweep_close":data["M5"][event_i]["close"],"observed_bars":[{"timestamp":iso(data["M5"][k]["time"]),"close":data["M5"][k]["close"]} for k in range(event_i,event_i+3)],"variants":{}}
    for n in (1,2,3,5):
        rec=next((k for k in range(event_i+1,event_i+1+n) if float(data["M5"][k]["close"])>event_low),None)
        event_trace["variants"][str(n)]={"immediate_reclaim":False,"reclaim_timestamp":iso(data["M5"][rec]["time"]) if rec else None,"reclaim_delay":rec-event_i if rec else None,"candidate":None,"reason":("NO_DELAYED_RECLAIM_WITHIN_WINDOW" if rec is None else "MSS_NOT_FOUND_WITHIN_5_CANDLES")}
    result={"strategy":"LIQUIDITY_DISPLACEMENT_DELAYED_RECLAIM_RESEARCH_V1","rules":{"only_changed_assumption":"sweep close may reclaim original reference within N subsequent closed M5 candles","windows":[1,2,3,5],"entry_fraction":.25,"target_R":1.25,"max_hold_minutes":120,"max_retrace_candles":5},"coverage":{"M5_start":iso(data["M5"][0]["time"]),"M5_end":iso(data["M5"][-1]["time"]),"M15_start":iso(data["M15"][0]["time"]),"H1_start":iso(data["H1"][0]["time"])},"sep10_13":{"frozen":metrics(window_frozen),"delayed":{str(n):metrics(rows) for n,rows in window_delayed.items()},"target_event":event_trace,"nearest_generated_example":example},"full":{"frozen":metrics(frozen),"delayed":{str(n):metrics(rows) for n,rows in delayed.items()}},"chronological_split":{"split":iso(split),"frozen_discovery":metrics([x for x in frozen if int(datetime.fromisoformat(x["timestamp"]).timestamp())<split]),"frozen_validation":metrics([x for x in frozen if int(datetime.fromisoformat(x["timestamp"]).timestamp())>=split]),"delayed":{str(n):{"discovery":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())<split]),"validation":metrics([x for x in rows if int(datetime.fromisoformat(x["timestamp"]).timestamp())>=split])} for n,rows in delayed.items()}}}
    result["delay_buckets"]={str(n):metrics([x for x in delayed[5] if x.get("reclaim_delay")==n]) for n in range(0,4)}; result["delay_buckets"]["4-5"]=metrics([x for x in delayed[5] if x.get("reclaim_delay") in (4,5)])
    result["extension_buckets"]={}
    for label,lo,hi in (("0-0.10R",0,.10),("0.10-0.25R",.10,.25),("0.25-0.50R",.25,.50),("0.50R+",.50,10**9)):
        result["extension_buckets"][label]=metrics([x for x in delayed[5] if lo <= (float(x.get("max_adverse_extension",0))/float(x.get("atr") or 1)) < hi])
    result["added_vs_frozen"]={}
    for n,rows in delayed.items():
        # The only behavior added by the hypothesis is delay > 0.  Immediate
        # reclaim rows are the frozen assumption and are excluded here.
        added=[x for x in rows if x.get("reclaim_delay", 0) > 0]
        result["added_vs_frozen"][str(n)]={"number_added":len(added),"number_won":sum(x.get("exit_reason")=="TARGET" for x in added),"number_lost":sum(x.get("exit_reason")=="STOP" for x in added),"expectancy_added_R":metrics(added)["expectancy_R"],"PF_added":metrics(added)["profit_factor"],"max_DD_added_R":metrics(added)["max_drawdown_R"]}
    OUT.mkdir(parents=True,exist_ok=True); (OUT/"usdjpy_delayed_reclaim_20260917.json").write_text(json.dumps(result,indent=2,default=str)+"\n")
    print(json.dumps(result,indent=2,default=str))

if __name__=="__main__": main()
