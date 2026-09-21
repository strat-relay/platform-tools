from __future__ import annotations
import csv,json,os,random,statistics,subprocess
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from paper_engine import _candle,atr
from simple_sr_candle_validate import levels_before,swing_points
from candle_sequence_discovery import patterns

ROOT=Path(__file__).resolve().parent
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
WINPY=r"C:\Python39\python.exe"
PREFIX="/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
SYMBOL="XAUUSDm"; DISCOVERY_END="2026-06"; H=(1,2,3,6,12,24); K=10

def v(b,k): return _candle(b,k)
def iso(t): return datetime.fromtimestamp(int(t),timezone.utc).isoformat()
def month(t): return datetime.fromtimestamp(int(t),timezone.utc).strftime("%Y-%m")
def pct(n,d): return 100*n/d if d else 0.0
def col(b): return "U" if v(b,"close")>v(b,"open") else "D" if v(b,"close")<v(b,"open") else "N"

def fetch():
    env=dict(os.environ,WINEPREFIX=PREFIX)
    p=subprocess.run([WINE,WINPY,"Z:"+str(ROOT/"historical_fetch_symbol.py"),SYMBOL],env=env,text=True,capture_output=True,timeout=180,check=True)
    return json.loads(p.stdout.splitlines()[-1])

def feature(bars,i):
    a=atr(bars[i-40:i])[-1] or 1e-9;out=[]
    for b in bars[i-12:i]:
        r=max(v(b,"high")-v(b,"low"),1e-9);body=abs(v(b,"close")-v(b,"open"))
        out += [body/a,r/a,(v(b,"high")-max(v(b,"open"),v(b,"close")))/a,(min(v(b,"open"),v(b,"close"))-v(b,"low"))/a,body/r,(v(b,"close")-v(b,"low"))/r,1 if col(b)=="U" else -1 if col(b)=="D" else 0]
    for n in (3,6,12):
        w=bars[i-n:i];rs=[v(x,"high")-v(x,"low") for x in w]
        out += [(v(w[-1],"close")-v(w[0],"open"))/a,sum(rs)/a,statistics.pstdev(rs)/a,sum(col(x)=="U" for x in w),sum(col(x)=="D" for x in w)]
    return out

def outcome(bars,i,a):
    now=v(bars[i-1],"close");out={}
    for h in H:
        w=bars[i:i+h];hi=max(v(x,"high") for x in w);lo=min(v(x,"low") for x in w);cl=v(w[-1],"close")
        out[f"up_{h}"]=(hi-now)/a;out[f"down_{h}"]=(now-lo)/a;out[f"net_{h}"]=(cl-now)/a;out[f"high_{h}"]=hi>v(bars[i-1],"high");out[f"low_{h}"]=lo<v(bars[i-1],"low")
    u=out["up_6"];d=out["down_6"];n=out["net_6"]
    out["outcome"]="STRONG_UP" if u>=1 and u>d else "STRONG_DOWN" if d>=1 and d>u else "UP_THEN_REJECT" if u>=1 and n<0 else "DOWN_THEN_RECLAIM" if d>=1 and n>0 else "RANGE" if max(u,d)<.75 else "EXPANSION" if max(u,d)>=1.5 else "FAILED_BREAKOUT" if (out["high_6"] and n<0) or (out["low_6"] and n>0) else "MIXED"
    return out

def standardize(x):
    m=[statistics.mean(c) for c in zip(*x)];s=[statistics.pstdev([r[j] for r in x]) or 1 for j in range(len(m))]
    return [[(z-m[j])/s[j] for j,z in enumerate(r)] for r in x]
def dist(a,b): return sum((x-y)**2 for x,y in zip(a,b))
def cluster(x,k):
    c=[x[int(i*len(x)/k)] for i in range(k)]
    for _ in range(8):
        g=[[] for _ in range(k)]
        for i,r in enumerate(x):g[min(range(k),key=lambda j:dist(r,c[j]))].append(i)
        nc=[[(sum(x[i][j] for i in q)/len(q) if q else c[z][j]) for j in range(len(x[0]))] for z,q in enumerate(g)]
        if all(dist(nc[j],c[j])<1e-7 for j in range(k)):break
        c=nc
    return [min(range(k),key=lambda j:dist(r,c[j])) for r in x],c
def metrics(rows):
    r=[x["net_6"] for x in rows];return {"n":len(rows),"up_pct":pct(sum(x>0 for x in r),len(r)),"down_pct":pct(sum(x<0 for x in r),len(r)),"expected_movement":statistics.mean(r) if r else 0,"median_up_excursion":statistics.median([x["up_6"] for x in rows]) if rows else 0,"median_down_excursion":statistics.median([x["down_6"] for x in rows]) if rows else 0,"strong_up_pct":pct(sum(x["outcome"]=="STRONG_UP" for x in rows),len(rows)),"strong_down_pct":pct(sum(x["outcome"]=="STRONG_DOWN" for x in rows),len(rows))}

def main():
    d=fetch();bars=d["M5"];m15=d["M15"];end=max(int(x["time"]) for x in bars[:-1]);start=end-184*86400;w=[x for x in bars if start<=int(x["time"])<end];pts=swing_points(m15,2);mt=[int(x["time"]) for x in m15];cache={};rows=[];vec=[]
    for i in range(40,len(w)-25):
        t=int(w[i-1]["time"]);a=atr(w[i-40:i])[-1] or 1e-9;mi=max(0,__import__("bisect").bisect_left(mt,t)-1);cache_key=mi//20
        if cache_key not in cache:cache[cache_key]=levels_before(m15[max(0,cache_key*20-480):cache_key*20],t,pts)
        f=feature(w,i);o=outcome(w,i,a);rows.append({"timestamp":iso(t),"month":month(t),"session":datetime.fromtimestamp(t,timezone.utc).hour,"pre_colors":"".join(col(x) for x in w[i-6:i]),"near_support":any(z["type"]=="SUPPORT" for z in cache[cache_key]),"near_resistance":any(z["type"]=="RESISTANCE" for z in cache[cache_key]),"named_patterns":patterns(w,i),**o});vec.append(f)
    di=[j for j,x in enumerate(rows) if x["month"]<=DISCOVERY_END];vi=[j for j,x in enumerate(rows) if x["month"]>DISCOVERY_END];raw=[vec[j] for j in di];means=[statistics.mean(c) for c in zip(*raw)];scales=[statistics.pstdev([r[j] for r in raw]) or 1 for j in range(len(means))];z=[[(q-means[j])/scales[j] for j,q in enumerate(r)] for r in raw];train=z[::max(1,len(z)//5000)];_,centers=cluster(train,K);labels=[min(range(K),key=lambda q:dist(r,centers[q])) for r in z];valz=[[(q-means[k])/scales[k] for k,q in enumerate(vec[j])] for j in vi];val_labels=[min(range(K),key=lambda q:dist(r,centers[q])) for r in valz];cands=[]
    for c in range(K):
        a=[di[q] for q,l in enumerate(labels) if l==c];b=[j for j,l in zip(vi,val_labels) if l==c]
        if len(a)>=50:
            common=Counter(rows[j]["pre_colors"] for j in a).most_common(1)[0][0]
            cands.append({"candidate_id":f"P{c:02d}","definition":f"Previous 12 candles match raw normalized-shape cluster {c}; most common preceding six-candle direction sequence is {common} (U=up, D=down, N=doji). No named pattern or future outcome used.","representative_prior_six":common,"discovery":metrics([rows[j] for j in a]),"validation":metrics([rows[j] for j in b]),"discovery_n":len(a),"validation_n":len(b),"monthly_validation":{m:metrics([rows[j] for j in b if rows[j]["month"]==m]) for m in sorted(set(rows[j]["month"] for j in b))}})
    cands=sorted(cands,key=lambda x:abs(x["discovery"]["expected_movement"]),reverse=True)[:10]
    diset=set(di); viset=set(vi); named={p:{"discovery":metrics([x for j,x in enumerate(rows) if j in diset and p in x["named_patterns"]]),"validation":metrics([x for j,x in enumerate(rows) if j in viset and p in x["named_patterns"]])} for p in ("BULLISH_ENGULFING","BEARISH_ENGULFING","MORNING_STAR","EVENING_STAR")}
    out={"symbol":SYMBOL,"paper_only":True,"period":{"start":iso(start),"end":iso(end),"discovery_end":DISCOVERY_END},"outcome_definitions":{"strong_threshold_atr":1,"range_threshold_atr":.75,"expansion_threshold_atr":1.5,"horizons":H},"baseline":{"discovery":metrics([rows[j] for j in di]),"validation":metrics([rows[j] for j in vi])},"candidates":cands,"named_candle_comparison":named,"outcome_counts":Counter(x["outcome"] for x in rows),"data_quality":["Features use only the preceding 3/6/12 completed candles.","Clusters were fit on discovery only and then assigned to validation.","M1 was not used in this pass; exact intrabar ordering remains unresolved."]}
    with open(ROOT/"raw_sequence_candidates.csv","w",newline="",encoding="utf-8") as f:wri=csv.DictWriter(f,fieldnames=sorted({k for r in rows for k in r}));wri.writeheader();wri.writerows(rows)
    (ROOT/"raw_sequence_discovery_results.json").write_text(json.dumps(out,indent=2,default=lambda x:dict(x) if isinstance(x,Counter) else x),encoding="utf-8")
    lines=["# Outcome-first raw candle sequence discovery","","Research only. No existing strategy modified.",f"\nObservations: {len(rows)} · discovery: {len(di)} · validation: {len(vi)}","","| Candidate | Discovery N | Discovery movement | Validation N | Validation movement |","|---|---:|---:|---:|---:|"]
    for x in cands:lines.append(f"| {x['candidate_id']} | {x['discovery_n']} | {x['discovery']['expected_movement']:.4f} | {x['validation_n']} | {x['validation']['expected_movement']:.4f} |")
    lines += ["","## Baseline",json.dumps(out["baseline"],indent=2),"","## Candidates",json.dumps(cands,indent=2),"","## Named candle comparison",json.dumps(named,indent=2),"","## Conclusion","These are descriptive raw-shape candidates only. A candidate that reverses sign or lacks validation support is data-mined, not robust."]
    (ROOT/"raw_sequence_discovery_summary.md").write_text("\n".join(lines),encoding="utf-8");print(json.dumps({"observations":len(rows),"baseline":out["baseline"],"candidates":cands},indent=2))
if __name__=="__main__":main()
