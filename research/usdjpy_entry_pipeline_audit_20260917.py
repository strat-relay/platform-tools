"""Research-only entry-depth/wait audit over completed sequential setups."""
import json, random, statistics, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.usdjpy_sequential_mss_20260917 import DATA, iso, Sequential, replay_seq
from research.usdjpy_delayed_reclaim_20260917 import outcome, metrics

ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"artifacts/audits/usdjpy_entry_pipeline_audit_20260917.json"
FRACTIONS=(0,.10,.15,.20,.25,.33,.50); WAITS=(1,2,3,5,8)

def entry(row,bars,fraction):
    d=bars[row['displacement_index']]; lo,hi=float(d['low']),float(d['high'])
    return float(row['direction']=='LONG')*(hi-(hi-lo)*fraction)+float(row['direction']=='SHORT')*(lo+(hi-lo)*fraction)
def sim(row,bars,fraction,wait):
    e=float(row['bos_index'] if 'bos_index' in row else row['displacement_index']); px=float(row['direction']=='LONG')
    price=float(bars[int(e)]['close']) if fraction==0 and 'bos_index' in row else entry(row,bars,fraction)
    start=int(row.get('bos_index',row['displacement_index']))+1; fi=None
    for k in range(start,min(len(bars),start+wait)):
        b=bars[k]; touch=float(b['low'])<=price if row['direction']=='LONG' else float(b['high'])>=price; held=float(b['close'])>=price if row['direction']=='LONG' else float(b['close'])<=price
        if touch and held:fi=k;break
    z=dict(row);z['entry']=price;z['filled']=fi is not None;z['fill_index']=fi;z['fill_price']=price if fi is not None else None
    z.update(outcome({'entry':price,'stop_loss':row['stop_loss'],'direction':row['direction']},bars,fi) if fi is not None else {'exit_reason':None,'exit_price':None,'realized_R':None,'duration_minutes':None,'MAE':None,'MFE':None,'target':None})
    return z
def stat(rows):
    m=metrics(rows); return {**m,'completed_setups':len(rows),'unfilled':len(rows)-sum(x.get('filled') for x in rows)}
def bootstrap(rs,n=10000):
    if not rs:return None
    random.seed(20260917); vals=[]
    for _ in range(n): vals.append(random.choice(rs))
    vals.sort();return {'n':n,'mean':statistics.mean(rs),'ci95':[vals[250],vals[9749]]}
def main():
    d=json.loads(DATA.read_text());bars=d['M5'];start=int(bars[40]['time']);end=int(bars[-26]['time']);split=start+2*(end-start)//3
    controls={'A_FROZEN_V1':replay_seq(Sequential(0,0),d,start,end),
              'B_IMMEDIATE_BOS_PLUS1':replay_seq(Sequential(0,0),d,start,end),
              'C_DELAYED_N2_BOS_PLUS2':replay_seq(Sequential(2,0),d,start,end)}
    base_keys={(x['timestamp'],x['direction']) for x in controls['A_FROZEN_V1']}
    completed={
      'A_FROZEN_V1':controls['A_FROZEN_V1'],
      'B_IMMEDIATE_BOS_PLUS1':controls['B_IMMEDIATE_BOS_PLUS1'] + [x for x in replay_seq(Sequential(0,1),d,start,end) if x.get('bos_delay',0)>0 and (x['timestamp'],x['direction']) not in base_keys],
      'C_DELAYED_N2_BOS_PLUS2':controls['C_DELAYED_N2_BOS_PLUS2'] + [x for x in replay_seq(Sequential(2,2),d,start,end) if x.get('bos_delay',0)>0 and (x['timestamp'],x['direction']) not in {(y['timestamp'],y['direction']) for y in controls['C_DELAYED_N2_BOS_PLUS2']}],
    }
    result={'sep10_trace':{},'populations':{},'bootstrap':{}}
    # Exact case study from the C population.
    t='2026-09-10T07:30:00+00:00'; c=[x for x in completed['C_DELAYED_N2_BOS_PLUS2'] if x['timestamp']==t and x['direction']=='LONG']; result['sep10_trace']=c[0] if c else None
    for name,rows in completed.items():
      base25=[sim(x,bars,.25,5) for x in rows]; cells={}
      for f in FRACTIONS:
       for w in WAITS:
        z=[sim(x,bars,f,w) for x in rows]; key=f'{f:.2f}|wait_{w}'; cells[key]={'fraction':f,'wait':w,'full':stat(z),'discovery':stat([x for x,zx in zip(z,rows) if int(datetime.fromisoformat(zx['timestamp']).timestamp())<split]),'validation':stat([x for x,zx in zip(z,rows) if int(datetime.fromisoformat(zx['timestamp']).timestamp())>=split])}
        fills=[x for x in z if x.get('filled')]; cf=[x for x in base25 if x.get('filled')]; cells[key]['incremental_vs_25_control']={'incremental_fills':len(fills)-len(cf),'incremental_expectancy_R':None,'incremental_PF':None}
      result['populations'][name]={'completed_setups':len(rows),'cells':cells}
    # Separate B added-fill lead against exact frozen key population.
    basekeys={(x['timestamp'],x['direction']) for x in completed['A_FROZEN_V1']}; b=completed['B_IMMEDIATE_BOS_PLUS1']; added=[x for x in b if (x['timestamp'],x['direction']) not in basekeys]; bf=[sim(x,bars,.25,5) for x in added];bf=[x for x in bf if x.get('filled')]
    rs=[float(x['realized_R']) for x in bf if x.get('realized_R') is not None]; cut=split; disc=[x for x in bf if int(datetime.fromisoformat(x['timestamp']).timestamp())<cut];val=[x for x in bf if int(datetime.fromisoformat(x['timestamp']).timestamp())>=cut]; result['bootstrap']['B_added_35_fills']={'count':len(bf),'discovery':stat(disc),'validation':stat(val),'validation_bootstrap_R':bootstrap([float(x['realized_R']) for x in val if x.get('realized_R') is not None])}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(result,indent=2,default=str)+'\n');print(json.dumps(result,indent=2,default=str))
if __name__=='__main__':main()
