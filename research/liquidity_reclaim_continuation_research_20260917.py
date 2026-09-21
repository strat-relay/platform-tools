"""Staged, research-only USDJPY liquidity-reclaim continuation study."""
import json, math, random, statistics, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_engine import atr

ROOT=Path(__file__).resolve().parents[1]; DATA=Path('/tmp/usdjpy_historical_20260917.json'); OUT=ROOT/'artifacts/audits/liquidity_reclaim_continuation_research_20260917.json'
M5=json.loads(DATA.read_text())['M5']; CONTRACT=json.loads(DATA.read_text())['contract']
BOUNDARIES={'discovery_end':'2026-03-31T23:59:59+00:00','tuning_start':'2026-04-01T00:00:00+00:00','tuning_end':'2026-06-30T23:59:59+00:00','final_start':'2026-07-01T00:00:00+00:00','final_end':'2026-09-17T08:55:00+00:00'}
EPOCH={k:int(datetime.fromisoformat(v).timestamp()) for k,v in BOUNDARIES.items()}

def ts(i):return int(M5[i]['time'])
def iso(t):return datetime.fromtimestamp(int(t),timezone.utc).isoformat()
def metrics(rows):
    rs=[x['r'] for x in rows if x.get('r') is not None];w=[r for r in rs if r>0];l=[r for r in rs if r<0];eq=peak=dd=0.;streak=longest=0
    for r in rs:eq+=r;peak=max(peak,eq);dd=max(dd,peak-eq);streak=streak+1 if r<0 else 0;longest=max(longest,streak)
    return {'setups':len(rows),'fills':len(rs),'win_rate_pct':100*len(w)/len(rs) if rs else 0,'expectancy_R':statistics.mean(rs) if rs else 0,'median_R':statistics.median(rs) if rs else 0,'profit_factor':sum(w)/abs(sum(l)) if l else None,'max_drawdown_R':dd,'max_losing_streak':longest}
def segment(t):
    if t<=EPOCH['discovery_end']:return 'discovery'
    if EPOCH['tuning_start']<=t<=EPOCH['tuning_end']:return 'tuning'
    return 'final_test' if t>=EPOCH['final_start'] else 'other'
def metrics(rows):
    rs=[float(x['r']) for x in rows if x.get('r') is not None]; wins=[r for r in rs if r>0];loss=[r for r in rs if r<0];eq=peak=dd=0.;streak=long=0
    for r in rs:eq+=r;peak=max(peak,eq);dd=max(dd,peak-eq);streak=streak+1 if r<0 else 0;long=max(long,streak)
    return {'setups':len(rows),'fills':len(rs),'win_rate_pct':100*len(wins)/len(rs) if rs else 0,'expectancy_R':statistics.mean(rs) if rs else 0,'median_R':statistics.median(rs) if rs else 0,'profit_factor':sum(wins)/abs(sum(loss)) if loss else None,'max_drawdown_R':dd,'max_losing_streak':long,'MAE_median_R':statistics.median([x['mae'] for x in rows if x.get('mae') is not None]) if rows else None,'MFE_median_R':statistics.median([x['mfe'] for x in rows if x.get('mfe') is not None]) if rows else None,'average_hold_minutes':statistics.mean([x['hold'] for x in rows if x.get('hold') is not None]) if rs else 0}
def outcome(entry,stop,direction,fill_i,target_r,hold):
    risk=entry-stop if direction=='LONG' else stop-entry
    if risk<=0:return None
    target=entry+risk*target_r if direction=='LONG' else entry-risk*target_r; win=M5[fill_i:min(len(M5),fill_i+max(1,int(hold/5)))]
    mae=((entry-min(float(x['low']) for x in win))/risk if direction=='LONG' else (max(float(x['high']) for x in win)-entry)/risk);mfe=((max(float(x['high']) for x in win)-entry)/risk if direction=='LONG' else (entry-min(float(x['low']) for x in win))/risk)
    for n,c in enumerate(win):
        sl=float(c['low'])<=stop if direction=='LONG' else float(c['high'])>=stop;tp=float(c['high'])>=target if direction=='LONG' else float(c['low'])<=target
        if sl or tp:return {'r':(stop-entry)/risk if direction=='LONG' and sl else (entry-stop)/risk if direction=='SHORT' and sl else target_r,'exit': 'STOP' if sl else 'TARGET','mae':mae,'mfe':mfe,'hold':n*5}
    px=float(win[-1]['close']);return {'r':(px-entry)/risk if direction=='LONG' else (entry-px)/risk,'exit':'TIME','mae':mae,'mfe':mfe,'hold':hold}
def detect(spec):
    rows=[];seen=set();lb=spec['lookback'];n=spec['reclaim_delay'];
    for i in range(max(120,lb+12),len(M5)-50):
        t=ts(i)
        if segment(t) not in ('discovery','tuning','final_test'):continue
        prior=M5[i-lb:i];ref_low=min(float(x['low']) for x in prior);ref_high=max(float(x['high']) for x in prior);c=M5[i];direction='LONG' if float(c['low'])<ref_low else 'SHORT' if float(c['high'])>ref_high else None
        if not direction:continue
        ref=ref_low if direction=='LONG' else ref_high;rec=i if (float(c['close'])>ref if direction=='LONG' else float(c['close'])<ref) else next((j for j in range(i+1,i+1+n) if (float(M5[j]['close'])>ref if direction=='LONG' else float(M5[j]['close'])<ref)),None)
        if rec is None:continue
        a=atr(M5[i-119:i+1])[-1];micro=max(float(x['high']) for x in M5[i-spec['structure']:i]) if direction=='LONG' else min(float(x['low']) for x in M5[i-spec['structure']:i]);disp=None
        for j in range(rec+1,rec+6):
            d=M5[j];rng=float(d['high'])-float(d['low']);b=abs(float(d['close'])-float(d['open']));loc=(float(d['close'])-float(d['low']))/rng if rng else .5;ok=b>=a*spec['disp_atr'] and (float(d['close'])>float(d['open']) and loc>=.6 if direction=='LONG' else float(d['close'])<float(d['open']) and loc<=.4)
            if ok:disp=j;break
        if disp is None:continue
        bos=next((j for j in range(disp,min(len(M5),disp+1+spec['bos_delay'])) if (float(M5[j]['close'])>micro if direction=='LONG' else float(M5[j]['close'])<micro)),None)
        if bos is None:continue
        key=(i,direction)
        if key in seen:continue
        seen.add(key);rows.append({'setup':f'{i}-{direction}','timestamp':iso(t),'direction':direction,'sweep_index':i,'reclaim_index':rec,'disp_index':disp,'bos_index':bos,'ref':ref,'micro':micro,'sweep_extreme':float(c['low'] if direction=='LONG' else c['high']),'atr':a,'structure_delay':bos-disp})
    return rows

def apply_entry(rows,spec):
    out=[]
    for x in rows:
        d=M5[x['disp_index']]; b=M5[x['bos_index']];lo,hi=float(d['low']),float(d['high']);direction=x['direction']
        if spec['entry']=='IMMEDIATE':price=float(b['close'])
        elif spec['entry']=='DISP_RETRACE':price=hi-(hi-lo)*spec['fraction'] if direction=='LONG' else lo+(hi-lo)*spec['fraction']
        elif spec['entry']=='BOS_RETRACE':bl,bh=float(b['low']),float(b['high']);price=bh-(bh-bl)*spec['fraction'] if direction=='LONG' else bl+(bh-bl)*spec['fraction']
        else:
            level=round(float(b['close'])*2)/2 if spec['round']=='500' else round(float(b['close']))
            price=level
        buffer=x['atr']*spec['stop_buffer'];stop=x['sweep_extreme']-buffer if direction=='LONG' else x['sweep_extreme']+buffer;fi=None
        for k in range(x['bos_index']+1,min(len(M5),x['bos_index']+1+spec['expiration'])):
            c=M5[k];touch=float(c['low'])<=price if direction=='LONG' else float(c['high'])>=price;held=float(c['close'])>=price if direction=='LONG' else float(c['close'])<=price
            if touch and held:fi=k;break
        z=dict(x);z.update({'entry':price,'filled':fi is not None,'fill_index':fi,'r':None,'exit':None,'mae':None,'mfe':None,'hold':None})
        if fi is not None:z.update(outcome(price,stop,direction,fi,spec['target_r'],spec['hold']) or {})
        out.append(z)
    return out
def bootstrap(rows,n=10000):
    vals=[x['r'] for x in rows if x.get('r') is not None]
    if not vals:return None
    r=random.Random(20260917); means=sorted(sum(r.choice(vals) for _ in vals)/len(vals) for _ in range(n));return {'n':n,'mean':statistics.mean(vals),'ci95':[means[250],means[9749]],'p_mean_gt_zero':sum(v>0 for v in means)/n}
def main():
    print('BOUNDARIES',json.dumps(BOUNDARIES,indent=2)); structural=[]
    for lb in (5,8,12):
      for reclaim in (0,2):
       for disp in (.5,.8): structural.append({'lookback':lb,'reclaim_delay':reclaim,'disp_atr':disp,'structure':5,'bos_delay':2,'entry':'DISP_RETRACE','fraction':.25,'expiration':5,'stop_buffer':.1,'target_r':1.25,'hold':120})
    scored=[]
    for s in structural:
        rows=apply_entry(detect(s),s); tm=metrics_with_spec(rows,s); scored.append({'spec':s,'metrics':tm})
    finalists=sorted(scored,key=lambda x:(x['metrics']['tuning']['expectancy_R'],x['metrics']['tuning']['profit_factor'] or 0),reverse=True)[:3]
    # Entry/exit research is applied only to the fixed finalist structural set.
    finalists_out=[]
    for f in finalists:
      base=f['spec']; rows=detect(base); entries=[]
      for e in ({'entry':'IMMEDIATE'},{'entry':'DISP_RETRACE','fraction':.15},{'entry':'DISP_RETRACE','fraction':.25},{'entry':'BOS_RETRACE','fraction':.15},{'entry':'BOS_RETRACE','fraction':.25},{'entry':'ROUND','round':'000'},{'entry':'ROUND','round':'500'}):
        s=dict(base);s.update(e);s.setdefault('fraction',.25);s.setdefault('round','000');s['expiration']=5
        z=apply_entry(rows,s);entries.append({'spec':s,'metrics':metrics_with_spec(z,s),'costs':{str(p):metrics_with_spec_cost(z,p) for p in (0,.5,1,1.5)},'bootstrap':bootstrap([x for x in z if x.get('r') is not None])})
      finalists_out.append({'structural':base,'entries':entries})
    result={'family':'LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH','boundaries':BOUNDARIES,'stage1_structural_variants_evaluated':len(structural),'stage1_screen':scored,'finalists':finalists_out,'selection_rule':'tuning-only ranking with sample/stability review; final test untouched until after selection','sep10_case_study':'evaluated after selection only'}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(result,indent=2,default=str)+'\n');print('WROTE',OUT)
def metrics_with_spec(rows,spec):
    return {'discovery':metrics([x for x in rows if segment(int(datetime.fromisoformat(x['timestamp']).timestamp()))=='discovery']),'tuning':metrics([x for x in rows if segment(int(datetime.fromisoformat(x['timestamp']).timestamp()))=='tuning']),'final_test':metrics([x for x in rows if segment(int(datetime.fromisoformat(x['timestamp']).timestamp()))=='final_test'])}
def metrics_with_spec_cost(rows,pips):
    z=[]
    for x in rows:
      if x.get('r') is None:continue
      risk=abs(x['entry']-(x['sweep_extreme']-x['atr']*.1 if x['direction']=='LONG' else x['sweep_extreme']+x['atr']*.1));y=dict(x);y['r']=x['r']-(float(M5[x['fill_index']]['spread'])*CONTRACT['point']+pips*.01)/risk;z.append(y)
    return metrics(z)
if __name__=='__main__':main()
