"""Pre-specified research comparison: 15%/5 versus frozen 25%/5."""
import json, random, statistics, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.usdjpy_sequential_mss_20260917 import DATA, Sequential, replay_seq
from research.usdjpy_entry_pipeline_audit_20260917 import sim, stat

ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'artifacts/audits/usdjpy_15pct_hypothesis_20260917.json'; SPLIT_EPOCH=None
def filled(rows): return [x for x in rows if x.get('filled') and x.get('realized_R') is not None]
def basic(rows):
    z=filled(rows); return {'fills':len(z),'expectancy_R':statistics.mean([x['realized_R'] for x in z]) if z else 0,'profit_factor':sum(x['realized_R'] for x in z if x['realized_R']>0)/abs(sum(x['realized_R'] for x in z if x['realized_R']<0)) if any(x['realized_R']<0 for x in z) else None,'win_rate_pct':100*sum(x['realized_R']>0 for x in z)/len(z) if z else 0,'max_drawdown_R':stat(z)['max_drawdown_R'] if z else 0}
def bootstrap(values,n=10000,seed=20260917):
    if not values:return None
    r=random.Random(seed); means=sorted(sum(r.choice(values) for _ in values)/len(values) for _ in range(n));return {'n':n,'mean':statistics.mean(values),'median':statistics.median(values),'ci95':[means[250],means[9749]],'probability_mean_gt_zero':sum(x>0 for x in means)/n}
def block_bootstrap(rows,n=10000):
    groups=defaultdict(list)
    for x in rows: groups[datetime.fromisoformat(x['timestamp']).strftime('%G-%V')].append(float(x['realized_R']))
    gs=list(groups.values())
    if not gs:return None
    r=random.Random(20260917); vals=[]
    for _ in range(n):
        chosen=[r.choice(gs) for _ in gs]; vals.append(sum(sum(g) for g in chosen)/sum(len(g) for g in chosen))
    vals.sort();return {'n':n,'ci95':[vals[250],vals[9749]],'probability_mean_gt_zero':sum(x>0 for x in vals)/n}
def cost(rows,extra_pips):
    z=[]
    for x in filled(rows):
        risk=float(x['risk']); base=float(x.get('spread',0)); c=(base+extra_pips*.01)/risk; y=dict(x); y['realized_R']=float(x['realized_R'])-c; z.append(y)
    return basic(z)
def main():
    d=json.loads(DATA.read_text()); bars=d['M5']; start=int(bars[40]['time']); end=int(bars[-26]['time']); split=start+2*(end-start)//3
    a=replay_seq(Sequential(0,0),d,start,end); keys={(x['timestamp'],x['direction']) for x in a}; b=a+[x for x in replay_seq(Sequential(0,1),d,start,end) if x.get('bos_delay',0)>0 and (x['timestamp'],x['direction']) not in keys]
    pops={'FROZEN_V1':a,'IMMEDIATE_BOS_PLUS1':b}; result={'controls':{'frozen_completed':len(a),'frozen_fills_25_5':sum(x.get('filled') for x in [sim(y,bars,.25,5) for y in a])},'populations':{},'factorial':{},'paired_frozen':{},'B_added_bootstrap':{}}
    simulated={}
    for pn,rows in pops.items():
      simulated[pn]={}
      for f in (.25,.15):
       z=[sim(x,bars,f,5) for x in rows];simulated[pn][f]=z
       result['populations'].setdefault(pn,{})[str(f)]={'full':stat(z),'discovery':stat([x for x in z if int(datetime.fromisoformat(x['timestamp']).timestamp())<split]),'validation':stat([x for x in z if int(datetime.fromisoformat(x['timestamp']).timestamp())>=split]),'costs':{str(p):cost(z,p) for p in (0,.5,1,1.5)}}
    # Pair categories and 15-vs-25 deltas on frozen population.
    p15=simulated['FROZEN_V1'][.15];p25=simulated['FROZEN_V1'][.25];cats=defaultdict(list)
    for x,y in zip(p15,p25):cats[('both' if x['filled'] and y['filled'] else '15_only' if x['filled'] else '25_only' if y['filled'] else 'neither')].append((x,y))
    result['paired_frozen']['categories']={k:len(v) for k,v in cats.items()}; result['paired_frozen']['containment_counterexamples']=sum(1 for x,y in cats['both'] if (x['direction']=='LONG' and x['entry']<=y['entry']) or (x['direction']=='SHORT' and x['entry']>=y['entry']))
    both=[(x,y) for x,y in cats['both']]; deltas=[float(x['realized_R'])-float(y['realized_R']) for x,y in both]; result['paired_frozen']['both_filled_delta_bootstrap']=bootstrap(deltas); result['paired_frozen']['both_filled_count']=len(both); result['paired_frozen']['15_only']=stat([x for x,y in cats['15_only']]); result['paired_frozen']['25_only']=stat([y for x,y in cats['25_only']])
    # Four-cell factorial summary and exact B incremental 35-fill lead.
    for pn in pops:
      for f in (.25,.15): result['factorial'][pn+'|'+str(f)] = basic(simulated[pn][f])
    added=[x for x in b if (x['timestamp'],x['direction']) not in keys]; added25=[sim(x,bars,.25,5) for x in added]; af=filled(added25); cut=lambda z:[x for x in z if int(datetime.fromisoformat(x['timestamp']).timestamp())<split]
    result['B_added_bootstrap']={'count':len(af),'discovery':basic(cut(af)),'validation':basic([x for x in af if x not in cut(af)]),'validation_bootstrap':bootstrap([float(x['realized_R']) for x in [x for x in af if int(datetime.fromisoformat(x['timestamp']).timestamp())>=split]]),'validation_block_bootstrap':block_bootstrap([x for x in af if int(datetime.fromisoformat(x['timestamp']).timestamp())>=split])}
    # Monthly and rolling three-month frozen 15/25 comparison.
    for pn in ['FROZEN_V1']:
      result['time_segments']={}
      for f in (.25,.15):
       z=simulated[pn][f]; months=defaultdict(list)
       for x in z: months[datetime.fromisoformat(x['timestamp']).strftime('%Y-%m')].append(x)
       result['time_segments'][str(f)]={'monthly':{k:basic(v) for k,v in sorted(months.items())}}
      ms=sorted(set(datetime.fromisoformat(x['timestamp']).strftime('%Y-%m') for x in simulated[pn][.25])); roll={}
      for i in range(2,len(ms)): 
       lo,hi=ms[i-2],ms[i]; roll[hi]={'25':basic([x for x in simulated[pn][.25] if lo<=datetime.fromisoformat(x['timestamp']).strftime('%Y-%m')<=hi]),'15':basic([x for x in simulated[pn][.15] if lo<=datetime.fromisoformat(x['timestamp']).strftime('%Y-%m')<=hi])}
      result['time_segments']['rolling_3_month']=roll
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(result,indent=2,default=str)+'\n');print(json.dumps(result,indent=2,default=str))
if __name__=='__main__':main()
