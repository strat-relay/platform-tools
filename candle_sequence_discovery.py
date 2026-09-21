from __future__ import annotations

import csv, json, os, statistics, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import _candle, atr
from simple_sr_candle_validate import levels_before, swing_points

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
WINPY = r"C:\Python39\python.exe"
SYMBOL = "XAUUSDm"
DISCOVERY_END = "2026-06"
HORIZONS = (1, 2, 3, 6, 12)

def v(b, k): return _candle(b, k)
def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def month(t): return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")
def pct(n, d): return 100*n/d if d else 0.0
def color(b):
    d=v(b,"close")-v(b,"open")
    return "BULLISH" if d>1e-9 else "BEARISH" if d<-1e-9 else "DOJI"
def session(t):
    h=datetime.fromtimestamp(int(t),timezone.utc).hour
    return "ASIA" if h<8 else "LONDON" if h<13 else "OVERLAP" if h<17 else "NEW_YORK" if h<22 else "OTHER"

def fetch():
    env=dict(os.environ,WINEPREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5")
    p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_symbol.py"),SYMBOL],env=env,text=True,capture_output=True,timeout=180,check=True)
    return json.loads(p.stdout.splitlines()[-1])

def patterns(bars,i):
    if i<2:return []
    a,b,c=bars[i-2:i+1]; ao,ac=v(a,"open"),v(a,"close"); bo,bc=v(b,"open"),v(b,"close"); co,cc=v(c,"open"),v(c,"close")
    out=[]; bb=abs(bc-bo); cb=abs(cc-co); rng=max(v(c,"high")-v(c,"low"),1e-9); atrv=atr(bars[max(0,i-40):i+1])[-1] or 0
    if bc<bo and cc>co and co<=bc and cc>=bo: out.append("BULLISH_ENGULFING")
    if bc>bo and cc<co and co>=bc and cc<=bo: out.append("BEARISH_ENGULFING")
    if ac<ao and bb<=min(atrv*.5,abs(ac-ao)*.5) and cc>co and cb>=atrv*.75 and cc>=(ao+ac)/2: out.append("MORNING_STAR")
    if ac>ao and bb<=min(atrv*.5,abs(ac-ao)*.5) and cc<co and cb>=atrv*.75 and cc<=(ao+ac)/2: out.append("EVENING_STAR")
    return out

def event_row(bars,i,pattern,levels,contract):
    b=bars[i]; sp=float(b.get("spread",0))*float(contract["point"]); entry=v(b,"close")+sp/2 if pattern.startswith("BULL") or pattern=="MORNING_STAR" else v(b,"close")-sp/2; atrv=atr(bars[max(0,i-40):i+1])[-1] or 0; low=[z for z in levels if z["type"]=="SUPPORT" and v(b,"low")<=z["zone_high"]+atrv*.25 and v(b,"high")>=z["zone_low"]-atrv*.25]; high=[z for z in levels if z["type"]=="RESISTANCE" and v(b,"low")<=z["zone_high"]+atrv*.25 and v(b,"high")>=z["zone_low"]-atrv*.25]; prior=["U" if color(x)=="BULLISH" else "D" if color(x)=="BEARISH" else "N" for x in bars[i-6:i]]; body=abs(v(b,"close")-v(b,"open")); rng=max(v(b,"high")-v(b,"low"),1e-9); row={"event_id":f"CSD-{int(b['time'])}-{pattern}","timestamp":iso(b["time"]),"month":month(b["time"]),"session":session(b["time"]),"pattern":pattern,"event_color":color(b),"prior2":"".join(prior[-2:]),"prior3":"".join(prior[-3:]),"prior6":"".join(prior),"prior6_return":v(bars[i-1],"close")/v(bars[i-6],"open")-1,"body_atr":body/max(atrv,1e-9),"body_range":body/rng,"close_location":(v(b,"close")-v(b,"low"))/rng,"near_support":bool(low),"near_resistance":bool(high),"sr_context":"SUPPORT" if low else "RESISTANCE" if high else "NONE","spread":sp,"atr":atrv}
    for h in HORIZONS:
        w=bars[i+1:i+1+h]
        if not w:continue
        close=v(w[-1],"close"); bid=close-float(w[-1].get("spread",0))*float(contract["point"])/2; highs=[v(x,"high") for x in w]; lows=[v(x,"low") for x in w]; direction="UP" if bid>entry else "DOWN" if bid<entry else "FLAT"; row[f"direction_{h}"]=direction; row[f"movement_{h}"]=bid-entry; row[f"mfe_{h}"]=max(highs)-entry if entry>0 else 0; row[f"mae_{h}"]=entry-min(lows); row[f"high_take_{h}"]=max(highs)>v(b,"high"); row[f"low_take_{h}"]=min(lows)<v(b,"low"); row[f"high_then_reject_{h}"]=row[f"high_take_{h}"] and bid<entry; row[f"low_then_reject_{h}"]=row[f"low_take_{h}"] and bid>entry
    return row

def metrics(rows,h=6):
    mv=[float(x[f"movement_{h}"]) for x in rows]; up=[x for x in mv if x>0]; down=[x for x in mv if x<0]
    return {"n":len(rows),"up_pct":pct(len(up),len(mv)),"down_pct":pct(len(down),len(mv)),"avg_movement":statistics.mean(mv) if mv else 0,"median_movement":statistics.median(mv) if mv else 0,"mfe":statistics.median(float(x[f"mfe_{h}"]) for x in rows) if rows else 0,"mae":statistics.median(float(x[f"mae_{h}"]) for x in rows) if rows else 0,"high_take_pct":pct(sum(x[f"high_take_{h}"] for x in rows),len(rows)),"low_take_pct":pct(sum(x[f"low_take_{h}"] for x in rows),len(rows))}

def main():
    data=fetch(); bars=data["M5"]; m15=data["M15"]; contract=data["contract"]; end=max(int(x["time"]) for x in bars[:-1]); start=end-184*86400; work=[x for x in bars if start<=int(x["time"])<end]; points=swing_points(m15,2); mt=[int(x["time"]) for x in m15]; cache={}; events=[]
    for i in range(40,len(work)-13):
        ps=patterns(work,i)
        if not ps:continue
        t=int(work[i]["time"]); mi=max(0,__import__("bisect").bisect_left(mt,t)-1)
        if mi not in cache:cache[mi]=levels_before(m15[max(0,mi-480):mi],t,points)
        for p in ps: events.append(event_row(work,i,p,cache[mi],contract))
    disc=[x for x in events if x["month"]<=DISCOVERY_END]; val=[x for x in events if x["month"]>DISCOVERY_END]; patterns_list=sorted(set(x["pattern"] for x in events)); contexts={"ALL":lambda x:True,"AT_SUPPORT":lambda x:x["near_support"],"AT_RESISTANCE":lambda x:x["near_resistance"],"NO_NEARBY_SR":lambda x:x["sr_context"]=="NONE"}
    seqs={"BULLISH_ENGULFING":lambda x:x["pattern"]=="BULLISH_ENGULFING","BEARISH_ENGULFING":lambda x:x["pattern"]=="BEARISH_ENGULFING","MORNING_STAR":lambda x:x["pattern"]=="MORNING_STAR","EVENING_STAR":lambda x:x["pattern"]=="EVENING_STAR","BULLISH_AFTER_2_DOWN":lambda x:x["pattern"]=="BULLISH_ENGULFING" and x["prior2"]=="DD","BEARISH_AFTER_2_UP":lambda x:x["pattern"]=="BEARISH_ENGULFING" and x["prior2"]=="UU","BULLISH_AFTER_3_DOWN":lambda x:x["pattern"]=="BULLISH_ENGULFING" and x["prior3"]=="DDD","BEARISH_AFTER_3_UP":lambda x:x["pattern"]=="BEARISH_ENGULFING" and x["prior3"]=="UUU"}
    results={"symbol":SYMBOL,"paper_only":True,"period":{"start":iso(start),"end":iso(end),"discovery_end":DISCOVERY_END},"patterns":patterns_list,"base_rates":{},"context":{},"sequences":{}}
    baseline={k:metrics([x for x in disc if contexts[k](x)]) for k in contexts}; results["base_rates"]["discovery"]=baseline; results["base_rates"]["validation"]={k:metrics([x for x in val if contexts[k](x)]) for k in contexts}
    for p in patterns_list:
        for c,fn in contexts.items():
            a=[x for x in disc if x["pattern"]==p and fn(x)]; b=[x for x in val if x["pattern"]==p and fn(x)]; results["context"].setdefault(p,{})[c]={"discovery":metrics(a),"validation":metrics(b)}
    for n,fn in seqs.items():
        a=[x for x in disc if fn(x)]; b=[x for x in val if fn(x)]; results["sequences"][n]={"discovery":metrics(a),"validation":metrics(b),"monthly_discovery":{m:metrics([x for x in a if x["month"]==m]) for m in sorted(set(x["month"] for x in a))},"monthly_validation":{m:metrics([x for x in b if x["month"]==m]) for m in sorted(set(x["month"] for x in b))}}
    # Candidate ranking is discovery-only and requires 40 observations; validation is never used for selection.
    ranked=sorted(((n,x) for n,x in results["sequences"].items() if x["discovery"]["n"]>=40),key=lambda z:abs(z[1]["discovery"]["up_pct"]-50),reverse=True); results["discovery_ranked_candidates"]=[n for n,_ in ranked]
    fields=sorted({k for r in events for k in r});
    with open(ROOT/"candle_sequence_discovery_samples.csv","w",newline="",encoding="utf-8") as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(events)
    (ROOT/"candle_sequence_discovery_results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    lines=["# XAUUSDm candle-to-candle pattern discovery","","Research only. Patterns are detected everywhere; S/R is context only.","",f"Events: {len(events)} · discovery: {len(disc)} · validation: {len(val)}","","## Pattern sequences", "| Pattern | Discovery N | Discovery up % | Validation N | Validation up % |", "|---|---:|---:|---:|---:|"]
    for n in results["discovery_ranked_candidates"][:10]:x=results["sequences"][n];lines.append(f"| {n} | {x['discovery']['n']} | {x['discovery']['up_pct']:.2f}% | {x['validation']['n']} | {x['validation']['up_pct']:.2f}% |")
    lines += ["","## Context comparison",json.dumps(results["context"],indent=2),"","## Conclusion","No pattern is promoted to a strategy. Discovery ranking is frozen before validation and small samples are not treated as evidence."]
    (ROOT/"candle_sequence_discovery_summary.md").write_text("\n".join(lines),encoding="utf-8");print(json.dumps({"events":len(events),"patterns":patterns_list,"ranked":results["discovery_ranked_candidates"]},indent=2))

if __name__=="__main__":main()
