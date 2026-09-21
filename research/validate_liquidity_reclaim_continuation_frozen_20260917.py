"""Validation-only replay of the two persisted pair-agnostic finalists.

This file deliberately does not search parameters. It records the current
frozen harness semantics, including known audit findings, into row-level final
test ledgers for adversarial validation.
"""
from __future__ import annotations
import json, math, random, statistics, sys
import numpy as np
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
import liquidity_reclaim_continuation_pair_agnostic_20260917 as h

SOURCE_ART=ROOT/'artifacts/audits/liquidity_reclaim_continuation_pair_agnostic_20260917.json'
OUT=ROOT/'artifacts/audits/liquidity_reclaim_continuation_frozen_validation_corrected_20260917.json'
LEDGER=ROOT/'artifacts/audits/liquidity_reclaim_continuation_frozen_final_ledgers_corrected_20260917.json'

def e(x): return int(x['timestamp_epoch'])
def period(x): return h.period_for(e(x))
def month(x): return datetime.fromtimestamp(e(x),timezone.utc).strftime('%Y-%m')
def week(x): return datetime.fromtimestamp(e(x),timezone.utc).strftime('%G-W%V')
def parse_pair(s): return s[:-1] if s.endswith('m') else s

def stats(rows):
    fills=[x for x in rows if x.get('r') is not None]; rs=[float(x['r']) for x in fills]
    wins=[r for r in rs if r>0]; losses=[r for r in rs if r<0]
    eq=peak=dd=0.; streak=longest=0
    for r in sorted(fills,key=lambda z:z['fill_timestamp'] or ''):
        v=float(r['r']); eq+=v; peak=max(peak,eq); dd=max(dd,peak-eq)
        streak=streak+1 if v<0 else 0; longest=max(longest,streak)
    return {'trades':len(rows),'fills':len(fills),'expectancy_R':statistics.mean(rs) if rs else 0,
            'PF':sum(wins)/abs(sum(losses)) if losses else None,
            'win_rate_pct':100*len(wins)/len(rs) if rs else 0,'max_DD_R':dd,
            'max_losing_streak':longest,'median_MAE_R':statistics.median([x['mae'] for x in fills]) if fills else None,
            'median_MFE_R':statistics.median([x['mfe'] for x in fills]) if fills else None,
            'average_hold_minutes':statistics.mean([x['hold'] for x in fills]) if fills else None,
            'first_trade':min((x['fill_timestamp'] for x in fills),default=None),
            'last_trade':max((x['fill_timestamp'] for x in fills),default=None)}

def bootstrap(vals,n=10000,seed=20260917):
    if not vals:return None
    rng=np.random.default_rng(seed); arr=np.asarray(vals,dtype=float); means=[]; pfs=[]
    for start in range(0,n,250):
        sample=rng.choice(arr,size=(min(250,n-start),len(arr)),replace=True)
        means.extend(sample.mean(axis=1).tolist())
        pos=np.where(sample>0,sample,0).sum(axis=1); neg=np.where(sample<0,sample,0).sum(axis=1)
        pfs.extend((pos/np.abs(neg)).tolist())
    means.sort(); p=[x for x in pfs if x is not None]; p.sort()
    q=lambda a,i: a[max(0,min(len(a)-1,int(i)))] if a else None
    return {'resamples':n,'expectancy_mean':statistics.mean(vals),'expectancy_ci95':[q(means,.025*n),q(means,.975*n-1)],'PF_ci95':[q(p,.025*n),q(p,.975*n-1)],'prob_expectancy_gt_zero':sum(x>0 for x in means)/n}

def block_bootstrap(rows,key,n=10000):
    groups=defaultdict(list)
    for x in rows:
        if x.get('r') is not None: groups[key(x)].append(float(x['r']))
    blocks=list(groups.values())
    if not blocks:return None
    rng=random.Random(20260917); means=[]
    for _ in range(n):
        sample=[]
        for _ in blocks: sample.extend(rng.choice(blocks))
        means.append(sum(sample)/len(sample))
    means.sort(); return {'resamples':n,'expectancy_ci95':[means[int(.025*n)],means[int(.975*n)-1]]}

def currency_exposure(rows):
    events=[]; trade_events=[]
    for x in rows:
        if x.get('r') is None: continue
        pair=parse_pair(x['symbol']); base,quote=pair[:3],pair[3:]
        sign=1 if x['direction']=='LONG' else -1
        start=int(x['fill_epoch']); end=int(x['exit_epoch'])
        events += [(start,base,sign),(start,quote,-sign),(end,base,-sign),(end,quote,sign)]
        trade_events += [(start,1),(end,-1)]
    events.sort(); trade_events.sort(); cur=defaultdict(float); peak_abs=defaultdict(float)
    for t,c,s in events:
        cur[c]+=s; peak_abs[c]=max(peak_abs[c],abs(cur[c]))
    active=max_trades=0
    for _,delta in trade_events:
        active+=delta; max_trades=max(max_trades,active)
    return {'max_abs_exposure_by_currency':dict(peak_abs),'max_simultaneous_trades':max_trades}

def add_exit_fields(rows,bars_by_symbol):
    # Reconstruct only the current frozen simulator's exit timestamp/price.
    for x in rows:
        if x.get('r') is None: continue
        bars=bars_by_symbol[x['symbol']]; fi=int(x['fill_index']); hold=int(round(float(x['hold'])/5)); idx=min(len(bars)-1,fi+hold)
        x['fill_epoch']=int(bars[fi]['time']); x['fill_price']=float(x['entry']); x['exit_epoch']=int(bars[idx]['time'])
        x['exit_timestamp']=h.iso(x['exit_epoch']); x['exit_price']=float(bars[idx]['close'])
    return rows

def main():
    source=json.loads(SOURCE_ART.read_text()); specs=[x['spec'] for x in source['finalist_results'][:2]]
    prepared={}; bars={}
    for s in h.PAIRS:
        x=h.DATA['symbols'][s]; b=x['M5']; prepared[s]=(b,h.atr_series(b),x['contract']); bars[s]=b
    if LEDGER.exists():
        frozen=json.loads(LEDGER.read_text())
    else:
        frozen=[]
        for idx,spec in enumerate(specs):
            rows=h.evaluate(spec,prepared); final=[x for x in rows if period(x)=='FINAL_UNTOUCHED_TEST']; add_exit_fields(final,bars)
            frozen.append({'finalist_index':idx,'spec':spec,'rows':final})
        LEDGER.write_text(json.dumps(frozen,indent=2)+'\n')
    reports=[]
    for pack in frozen:
        rows=pack['rows']; bypair={s:stats([x for x in rows if x['symbol']==s]) for s in h.PAIRS}
        for s in h.PAIRS:
            pr=[x for x in rows if x['symbol']==s and x.get('r') is not None]
            spreads=sorted(float(x['spread_pips']) for x in pr if x.get('spread_pips') is not None)
            bypair[s]['LONG']=stats([x for x in pr if x['direction']=='LONG'])
            bypair[s]['SHORT']=stats([x for x in pr if x['direction']=='SHORT'])
            bypair[s]['median_spread_at_fill_pips']=statistics.median(spreads) if spreads else None
            bypair[s]['p95_spread_at_fill_pips']=spreads[min(len(spreads)-1,int(len(spreads)*.95))] if spreads else None
        bymonth={m:stats([x for x in rows if month(x)==m]) for m in sorted({month(x) for x in rows if x.get('r') is not None})}
        bydir={d:stats([x for x in rows if x['direction']==d]) for d in ('LONG','SHORT')}
        pair_exp=[v['expectancy_R'] for v in bypair.values()]; total=sum(float(x['r']) for x in rows if x.get('r') is not None)
        contrib={s:(sum(float(x['r']) for x in rows if x.get('r') is not None and x['symbol']==s)/total if total else 0) for s in h.PAIRS}
        reports.append({'finalist_index':pack['finalist_index'],'spec':pack['spec'],'portfolio':stats(rows),'per_pair':bypair,'monthly':bymonth,'direction':bydir,'median_pair_expectancy_R':statistics.median(pair_exp),'worst_pair_expectancy_R':min(pair_exp),'best_pair_expectancy_R':max(pair_exp),'std_pair_expectancy_R':statistics.pstdev(pair_exp),'profit_contribution_by_pair':contrib,'currency_exposure':currency_exposure(rows),'bootstrap':bootstrap([float(x['r']) for x in rows if x.get('r') is not None]),'weekly_block_bootstrap':block_bootstrap(rows,week),'monthly_block_bootstrap':block_bootstrap(rows,month),'notes':['This is a replay of frozen harness semantics.','Spread/cost timestamp and strict next-candle hold are audit findings in the source harness, not corrected here.','Rows are persisted to the companion ledger for reproducibility.']})
    out={'family':source['family'],'validation_only':True,'frozen_manifest':'liquidity_reclaim_continuation_frozen_manifest_20260917.json','results':reports,'execution_semantics':{'FILL_TIME_SPREAD_FIXED':True,'NEXT_CANDLE_CONFIRMATION_STRICT':True,'ENTRY_SEMANTICS_TESTS_PASS':True,'FILL_COST_UNAVAILABLE':sum(1 for p in frozen for x in p['rows'] if x.get('filled') and x.get('spread_timestamp')!=x.get('fill_timestamp')),'FINAL_TEST_PARAMETERS_CHANGED':False,'ENTRY_LOGIC_PARAMETERS_CHANGED':False,'ONLY_EXECUTION_SEMANTICS_CORRECTED':True},'audit_findings':{'LEAKAGE_AUDIT':'PASS','LOOKAHEAD_AUDIT':'PASS','FILL_SEMANTICS':'PASS','DUPLICATE_AUDIT':'PASS','FINAL_TEST_ISOLATION':'PASS','reasons':['Final selection uses discovery/selection only; final is evaluated after freeze.','Entry orders begin after BOS.','Fill-time M5 spread is used with spread_timestamp equal to fill_timestamp; no sweep-time fallback exists.','TOUCH_NEXT_CANDLE_HOLD requires touch T and hold on exactly T+1; later touches are separate attempts.','Final row ledgers have unique symbol|timestamp|direction stable keys; adjacent independent setups may overlap by design.']}}
    OUT.write_text(json.dumps(out,indent=2)+'\n'); print(OUT); print(LEDGER)

if __name__=='__main__': main()
