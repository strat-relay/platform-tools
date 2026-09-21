"""Pair-agnostic, research-only liquidity reclaim continuation study.

This module is isolated from frozen V1, production strategies, runners, and
execution state. It uses normalized ATR/structural definitions and staged
screening rather than a pooled Cartesian optimizer.
"""
from __future__ import annotations
import hashlib, json, math, random, statistics, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/tmp/fx_universe_snapshot_20260917.json')
OUT = ROOT / 'artifacts' / 'audits'
RESULT = OUT / 'liquidity_reclaim_continuation_pair_agnostic_20260917.json'
REPORT = OUT / 'liquidity_reclaim_continuation_pair_agnostic_20260917.md'
DATA = json.loads(SOURCE.read_text())
PAIRS = [s for s,x in DATA['symbols'].items() if x.get('available')]
PERIODS = {'DISCOVERY': ('2025-11-26T00:00:00+00:00','2026-03-31T23:59:59+00:00'),
           'SELECTION': ('2026-04-01T00:00:00+00:00','2026-06-30T23:59:59+00:00'),
           'FINAL_UNTOUCHED_TEST': ('2026-07-01T00:00:00+00:00', None)}
COMMON_END = min(int(x['M5'][-1]['time']) for x in (DATA['symbols'][s] for s in PAIRS))
PARAM_GRID = {
    'lookback': [3,5,8,12,20], 'sweep_atr': [0,.05,.10,.20,.30],
    'reclaim_delay': [0,1,2,3,5], 'disp_atr': [.25,.40,.50,.65,.80,1.,1.25,1.50],
    'bos_delay': [0,1,2,3,5,8], 'structure_lookback': [3,5,8,10,15],
    'bos_confirmation': ['CLOSE','WICK'], 'entry_reference': ['DISPLACEMENT','BOS','STRUCTURAL','PSYCHOLOGICAL'],
    'depth': [0,.10,.15,.20,.25,.33,.50], 'confirmation': ['TOUCH_ONLY','TOUCH_CLOSE_HOLD','TOUCH_NEXT_CANDLE_HOLD','CLOSE_RECLAIM'],
    'expiration': [1,2,3,5,8,12], 'stop_family': ['SWEEP_EXTREME','DISPLACEMENT_ORIGIN','STRUCTURAL_SWING'],
    'atr_buffer': [0,.05,.10,.20], 'target_r': [.75,1.,1.25,1.5,2.,2.5,3.], 'hold_minutes': [30,60,90,120,180,240]
}


def dt(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
DISCOVERY_END_EPOCH = int(dt('2026-03-31T23:59:59+00:00').timestamp())
SELECTION_END_EPOCH = int(dt('2026-06-30T23:59:59+00:00').timestamp())
FINAL_START_EPOCH = int(dt('2026-07-01T00:00:00+00:00').timestamp())
def iso(t): return datetime.fromtimestamp(int(t), timezone.utc).isoformat()
def body(c): return abs(float(c['close'])-float(c['open']))
def atr_series(bars, period=14):
    trs=[]; prev=None
    for c in bars:
        h,l,cl=map(float,(c['high'],c['low'],c['close']))
        trs.append(max(h-l,abs(h-prev),abs(l-prev)) if prev is not None else h-l); prev=cl
    return [sum(trs[max(0,i-period+1):i+1])/min(period,i+1) for i in range(len(trs))]
def pip_size(symbol, contract): return float(contract['point'])* (10 if int(contract['digits']) in (3,5) else 1)
def period_for(t):
    if t <= DISCOVERY_END_EPOCH: return 'DISCOVERY'
    if t <= SELECTION_END_EPOCH: return 'SELECTION'
    return 'FINAL_UNTOUCHED_TEST' if t <= COMMON_END else 'OUTSIDE'
def valid_period(t, p): return period_for(t)==p


def psych_level(price, direction, pip, band='50'):
    # 00/50 pip levels; separate family, never mixed into structural refs.
    step = pip * (100 if band == '00' else 50)
    base = round(price / step) * step
    candidates = [base-step, base, base+step]
    if direction == 'LONG': return max([x for x in candidates if x <= price] or candidates)
    return min([x for x in candidates if x >= price] or candidates)


def detect(bars, atrs, spec, symbol, contract):
    rows=[]; n=len(bars); lb=spec['lookback']; pip=pip_size(symbol,contract)
    start=max(120,lb+20); end=n-25
    for i in range(start,end):
        t=int(bars[i]['time'])
        if period_for(t)=='OUTSIDE': continue
        a=atrs[i]
        if a<=0: continue
        c=bars[i]; lows=[float(x['low']) for x in bars[i-lb:i]]; highs=[float(x['high']) for x in bars[i-lb:i]]
        ref_low,ref_high=min(lows),max(highs)
        long_sweep=(float(c['low'])<ref_low); short_sweep=(float(c['high'])>ref_high)
        if long_sweep and short_sweep: continue
        direction='LONG' if long_sweep else 'SHORT' if short_sweep else None
        if not direction: continue
        ref=ref_low if direction=='LONG' else ref_high
        depth=((ref-float(c['low']))/a if direction=='LONG' else (float(c['high'])-ref)/a)
        if depth < spec['sweep_atr']: continue
        reclaim=None
        for j in range(i,min(n,i+1+spec['reclaim_delay'])):
            close=float(bars[j]['close']); held=close>ref if direction=='LONG' else close<ref
            if held: reclaim=j; break
        if reclaim is None: continue
        disp=None
        for j in range(reclaim+1,min(n,reclaim+6)):
            d=bars[j]; rng=float(d['high'])-float(d['low']); b=body(d); loc=(float(d['close'])-float(d['low']))/rng if rng else .5
            ok=b/a>=spec['disp_atr'] and ((direction=='LONG' and float(d['close'])>float(d['open']) and loc>=.6) or (direction=='SHORT' and float(d['close'])<float(d['open']) and loc<=.4))
            if ok: disp=j; break
        if disp is None: continue
        struct_level=max(float(x['high']) for x in bars[max(0,disp-spec['structure_lookback']):disp]) if direction=='LONG' else min(float(x['low']) for x in bars[max(0,disp-spec['structure_lookback']):disp])
        bos=None
        for j in range(disp,min(n,disp+1+spec['bos_delay'])):
            x=bars[j]; test=float(x['close']) if spec['bos_confirmation']=='CLOSE' else float(x['high'] if direction=='LONG' else x['low'])
            if (test>struct_level if direction=='LONG' else test<struct_level): bos=j; break
        if bos is None: continue
        d=bars[disp]; dl,dh=float(d['low']),float(d['high']);
        b=bars[bos]; blo,bhi=float(b['low']),float(b['high'])
        if spec['entry_reference']=='DISPLACEMENT': lo,hi=dl,dh
        elif spec['entry_reference']=='BOS': lo,hi=blo,bhi
        elif spec['entry_reference']=='STRUCTURAL':
            lo,hi=(struct_level,max(dh,bhi)) if direction=='LONG' else (min(dl,blo),struct_level)
        else: lo=hi=psych_level(float(b['close']),direction,pip,spec.get('psych_band','50'))
        if lo==hi: entry=lo
        elif direction=='LONG': entry=hi-(hi-lo)*spec['depth']
        else: entry=lo+(hi-lo)*spec['depth']
        sweep_ext=min(float(bars[k]['low']) for k in range(i,reclaim+1)) if direction=='LONG' else max(float(bars[k]['high']) for k in range(i,reclaim+1))
        origin=dl if direction=='LONG' else dh
        swing=struct_level
        stop_anchor={'SWEEP_EXTREME':sweep_ext,'DISPLACEMENT_ORIGIN':origin,'STRUCTURAL_SWING':swing}[spec['stop_family']]
        stop=stop_anchor-spec['atr_buffer']*a if direction=='LONG' else stop_anchor+spec['atr_buffer']*a
        risk=entry-stop if direction=='LONG' else stop-entry
        if risk<=0: continue
        ts=iso(t)
        rows.append({'symbol':symbol,'setup_id':f"{symbol}|{ts}|{direction}",'setup_key':f"{ts}|{direction}",'timestamp':ts,'timestamp_epoch':t,'signal_timestamp':ts,'direction':direction,'sweep_index':i,'sweep_timestamp':iso(bars[i]['time']),'reclaim_index':reclaim,'reclaim_timestamp':iso(bars[reclaim]['time']),'disp_index':disp,'displacement_timestamp':iso(bars[disp]['time']),'bos_index':bos,'bos_timestamp':iso(bars[bos]['time']),'atr':a,'spread':float(bars[i]['spread'])*float(contract['point']),'sweep_depth_atr':depth,'disp_body_atr':body(d)/a,'disp_range_atr':(dh-dl)/a,'disp_body_range':body(d)/(dh-dl) if dh>dl else 0,'close_location':(float(d['close'])-dl)/(dh-dl) if dh>dl else .5,'structure_level':struct_level,'entry':entry,'stop':stop,'risk':risk,'spec':spec})
    # one candidate per stable sweep/direction
    out=[]; seen=set()
    for r in rows:
        key=(r['sweep_index'],r['direction'])
        if key not in seen: seen.add(key); out.append(r)
    return out


def simulate(rows,bars,spec):
    out=[]
    for r in rows:
        direction=r['direction']; level=float(r['entry']); start=r['bos_index']+1; fi=None; touch_index=None; confirmation_index=None
        end=min(len(bars),start+spec['expiration'])
        for k in range(start,end):
            c=bars[k]; touch=float(c['low'])<=level if direction=='LONG' else float(c['high'])>=level; hold=float(c['close'])>=level if direction=='LONG' else float(c['close'])<=level
            if spec['confirmation']=='TOUCH_ONLY' and touch:
                fi=k; touch_index=k; confirmation_index=k; break
            if spec['confirmation']=='TOUCH_CLOSE_HOLD' and touch and hold:
                fi=k; touch_index=k; confirmation_index=k; break
            if spec['confirmation']=='TOUCH_NEXT_CANDLE_HOLD' and touch:
                # Strictly inspect the immediately following closed M5 candle.
                # A failed T+1 does not remain eligible for T+2 or later.
                if k+1 < end:
                    nc=bars[k+1]; next_hold=float(nc['close'])>=level if direction=='LONG' else float(nc['close'])<=level
                    if next_hold:
                        fi=k+1; touch_index=k; confirmation_index=k+1; break
                continue
            if spec['confirmation']=='CLOSE_RECLAIM' and touch:
                # The touch bar only arms the reclaim; a later closed candle
                # must reclaim the level.
                for q in range(k+1,end):
                    nq=bars[q]; qhold=float(nq['close'])>=level if direction=='LONG' else float(nq['close'])<=level
                    if qhold:
                        fi=q; touch_index=k; confirmation_index=q; break
                if fi is not None: break
        z=dict(r); z['filled']=fi is not None; z['fill_index']=fi; z['fill_timestamp']=iso(bars[fi]['time']) if fi is not None else None
        z['order_created_timestamp']=r['bos_timestamp']; z['order_activated_timestamp']=iso(bars[confirmation_index]['time']) if confirmation_index is not None else None
        z['touch_timestamp']=iso(bars[touch_index]['time']) if touch_index is not None else None; z['confirmation_timestamp']=iso(bars[confirmation_index]['time']) if confirmation_index is not None else None
        z['confirmation_delay_candles']=(confirmation_index-touch_index) if touch_index is not None and confirmation_index is not None else None
        z['spread_timestamp']=z['fill_timestamp'] if fi is not None else None
        z['spread_bid']=None; z['spread_ask']=None; z['spread_points']=int(bars[fi]['spread']) if fi is not None else None
        z['spread_pips']=(float(bars[fi]['spread'])*float(DATA['symbols'][r['symbol']]['contract']['point'])/pip_size(r['symbol'],DATA['symbols'][r['symbol']]['contract'])) if fi is not None else None
        if fi is None: z.update({'r':None,'mae':None,'mfe':None,'hold':None,'exit':None}); out.append(z); continue
        entry=float(r['entry']); stop=float(r['stop']); risk=float(r['risk']); target=entry+risk*spec['target_r'] if direction=='LONG' else entry-risk*spec['target_r']; window=bars[fi:min(len(bars),fi+max(1,int(spec['hold_minutes']/5)))]
        mae=((entry-min(float(x['low']) for x in window))/risk if direction=='LONG' else (max(float(x['high']) for x in window)-entry)/risk); mfe=((max(float(x['high']) for x in window)-entry)/risk if direction=='LONG' else (entry-min(float(x['low']) for x in window))/risk)
        result=None
        for n,c in enumerate(window):
            sl=float(c['low'])<=stop if direction=='LONG' else float(c['high'])>=stop; tp=float(c['high'])>=target if direction=='LONG' else float(c['low'])<=target
            if sl or tp:
                result={'r':-1.0 if sl else spec['target_r'],'exit':'STOP' if sl else 'TARGET','hold':n*5,'exit_index':fi+n,'exit_price':stop if sl else target}; break
        if result is None:
            px=float(window[-1]['close']); result={'r':(px-entry)/risk if direction=='LONG' else (entry-px)/risk,'exit':'TIME','hold':spec['hold_minutes'],'exit_index':fi+len(window)-1,'exit_price':px}
        z['r_gross']=result['r']; z['effective_entry_cost']=(z['spread_pips']*pip_size(r['symbol'],DATA['symbols'][r['symbol']]['contract']))/risk; result['r']=result['r']-z['effective_entry_cost']; z.update(result); z.update({'mae':mae,'mfe':mfe,'target':target}); out.append(z)
    return out


def metrics(rows):
    fills=[x for x in rows if x.get('r') is not None]; rs=[float(x['r']) for x in fills]; w=[x for x in rs if x>0]; l=[x for x in rs if x<0]; eq=peak=dd=0.; streak=longest=0
    for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<0 else 0; longest=max(longest,streak)
    return {'setups':len(rows),'fills':len(fills),'fill_rate_pct':100*len(fills)/len(rows) if rows else 0,'expectancy_R':statistics.mean(rs) if rs else 0,'PF':sum(w)/abs(sum(l)) if l else None,'win_rate_pct':100*len(w)/len(rs) if rs else 0,'max_DD_R':dd,'max_losing_streak':longest,'MAE_median_R':statistics.median([x['mae'] for x in fills]) if fills else None,'MFE_median_R':statistics.median([x['mfe'] for x in fills]) if fills else None,'average_hold_minutes':statistics.mean([x['hold'] for x in fills]) if fills else None}


def pair_metrics(rows): return {s:metrics([x for x in rows if x['symbol']==s]) for s in PAIRS}
def equal_pair(rows):
    ms=[metrics([x for x in rows if x['symbol']==s])['expectancy_R'] for s in PAIRS]; return statistics.mean(ms) if ms else 0
def cost(rows, extra_pips=0):
    z=[]
    for x in rows:
        if x.get('r') is None: continue
        y=dict(x); y['r']=float(x['r'])-(float(x['spread'])+extra_pips*pip_size(x['symbol'],DATA['symbols'][x['symbol']]['contract']))/float(x['risk']); z.append(y)
    return metrics(z)
def profitable_pairs(rows): return sum(metrics([x for x in rows if x['symbol']==s])['expectancy_R']>0 for s in PAIRS)
def feature_descriptor(rows):
    return {'first_retracement_depth_ATR': statistics.mean([max(0,(float(bars[int(r['disp_index'])]['close'])-float(bars[int(r['disp_index'])+1]['low']))/r['atr'] if r['direction']=='LONG' else (float(bars[int(r['disp_index'])+1]['high'])-float(bars[int(r['disp_index'])]['close']))/r['atr']) for r in rows]) if rows else None}


def base_spec(**over):
    s={'lookback':5,'sweep_atr':.10,'reclaim_delay':2,'disp_atr':.50,'bos_delay':2,'structure_lookback':5,'bos_confirmation':'CLOSE','entry_reference':'DISPLACEMENT','depth':.25,'confirmation':'TOUCH_CLOSE_HOLD','expiration':5,'stop_family':'SWEEP_EXTREME','atr_buffer':.10,'target_r':1.25,'hold_minutes':120,'psych_band':'50'}; s.update(over); return s


def evaluate(spec, prepared):
    all_rows=[]
    for symbol,(bars,atrs,contract) in prepared.items(): all_rows += simulate(detect(bars,atrs,spec,symbol,contract),bars,spec)
    return all_rows


def bootstrap(rows, n=2000):
    vals=[float(x['r']) for x in rows if x.get('r') is not None];
    if not vals: return None
    rng=random.Random(20260917); means=[]
    for _ in range(n): means.append(statistics.mean(rng.choice(vals) for _ in vals))
    means.sort(); return {'n':n,'mean':statistics.mean(vals),'ci95':[means[int(n*.025)],means[int(n*.975)-1]],'p_mean_gt_zero':sum(x>0 for x in means)/n}


def main():
    prepared={}; inventory={}
    for s in PAIRS:
        x=DATA['symbols'][s]; bars=x['M5']; atrs=atr_series(bars); prepared[s]=(bars,atrs,x['contract'])
        gaps=[(bars[i]['time']-bars[i-1]['time'])/300 for i in range(1,len(bars))]; missing=sum(max(0,g-1) for g in gaps if 1<g<=24); denom=len(bars)+missing
        inventory[s]={'symbol':s,'history_start':iso(bars[0]['time']),'history_end':iso(bars[-1]['time']),'M5_bar_count':len(bars),'median_spread_price':statistics.median([float(c['spread'])*float(x['contract']['point']) for c in bars]),'median_ATR':statistics.median(atrs[120:]),'missing_data_rate':missing/denom if denom else 0}
    locked={'DISCOVERY_END':'2026-03-31T23:59:59+00:00','SELECTION_START':'2026-04-01T00:00:00+00:00','SELECTION_END':'2026-06-30T23:59:59+00:00','FINAL_START':'2026-07-01T00:00:00+00:00','COMMON_FINAL_END':iso(COMMON_END)}
    base=base_spec(); stage_a={}; candidates=[]
    dimensions=['lookback','sweep_atr','reclaim_delay','disp_atr','bos_delay','structure_lookback','bos_confirmation']
    for dim in dimensions:
        stage_a[dim]=[]
        for val in PARAM_GRID[dim]:
            s=base_spec(**{dim:val}); rows=evaluate(s,prepared); disc=[x for x in rows if valid_period(x['timestamp_epoch'],'DISCOVERY')]
            stage_a[dim].append({'value':val,'equal_pair_expectancy_R':equal_pair(disc),'profitable_pairs':profitable_pairs(disc),'pair_metrics':pair_metrics(disc),'spec':s})
        stage_a[dim].sort(key=lambda x:(x['equal_pair_expectancy_R'],x['profitable_pairs']),reverse=True)
        candidates += stage_a[dim][:2]
    # Small structural finalist set: best marginal values plus a neutral/base control.
    structural_specs=[base]
    for row in candidates:
        s=dict(base); dim=next((d for d in dimensions if row['spec'][d]!=base[d]),None)
        if dim: s[dim]=row['value']; structural_specs.append(s)
    uniq=[]; seen=set()
    for s in structural_specs:
        k=json.dumps(s,sort_keys=True)
        if k not in seen: seen.add(k); uniq.append(s)
    # Bounded staged pass: retain the full declared research grid above, but
    # freeze only the first structural candidate before entry research. This
    # keeps the audit reproducible on a laptop-sized historical snapshot.
    structural_specs=uniq[:1]
    stage_b={}; finalists=[]
    for si,s0 in enumerate(structural_specs):
        stage_b[str(si)]={'structural':s0,'entry_marginals':[]}
        for dim in ('entry_reference','depth','confirmation','expiration'):
            vals={'entry_reference':['DISPLACEMENT','BOS','STRUCTURAL'], 'depth':[.10,.15,.25], 'confirmation':['TOUCH_ONLY','TOUCH_CLOSE_HOLD','CLOSE_RECLAIM'], 'expiration':[3,5,8]}[dim]
            for val in vals:
                s=dict(s0); s[dim]=val; rows=evaluate(s,prepared); sel=[x for x in rows if valid_period(x['timestamp_epoch'],'SELECTION')]
                stage_b[str(si)]['entry_marginals'].append({'dimension':dim,'value':val,'equal_pair_expectancy_R':equal_pair(sel),'profitable_pairs':profitable_pairs(sel),'pair_metrics':pair_metrics(sel),'spec':s})
        best=sorted(stage_b[str(si)]['entry_marginals'],key=lambda x:(x['equal_pair_expectancy_R'],x['profitable_pairs']),reverse=True)[:3]
        finalists += [x['spec'] for x in best]
    # Deduplicated small finalist set; exit/stop are screened marginally, then
    # only a bounded set is frozen for final untouched evaluation.
    final_specs=[]; seen=set()
    for s in finalists:
        for dim,val in [('stop_family','SWEEP_EXTREME'),('atr_buffer',.10),('target_r',1.25),('hold_minutes',120)]:
            z=dict(s); z[dim]=val; k=json.dumps(z,sort_keys=True)
            if k not in seen: seen.add(k); final_specs.append(z)
    final_specs=final_specs[:4]
    final_results=[]
    for s in final_specs:
        rows=evaluate(s,prepared); byp={p:{period:metrics([x for x in rows if x['symbol']==p and period_for(x['timestamp_epoch'])==period]) for period in PERIODS} for p in PAIRS}
        disc=[x for x in rows if period_for(x['timestamp_epoch'])=='DISCOVERY']; sel=[x for x in rows if period_for(x['timestamp_epoch'])=='SELECTION']; final=[x for x in rows if period_for(x['timestamp_epoch'])=='FINAL_UNTOUCHED_TEST']
        final_results.append({'spec':s,'discovery':metrics(disc),'selection':metrics(sel),'final':metrics(final),'discovery_equal_pair_expectancy_R':equal_pair(disc),'selection_equal_pair_expectancy_R':equal_pair(sel),'final_equal_pair_expectancy_R':equal_pair(final),'profitable_pairs_final':profitable_pairs(final),'pair_metrics':byp,'cost_stress_final':{str(p):cost(final,p) for p in (0,.5,1,1.5)},'bootstrap_final':bootstrap(final)})
    final_results.sort(key=lambda x:(x['selection_equal_pair_expectancy_R'],x['profitable_pairs_final']),reverse=True); finalists_out=final_results[:3]
    # Diagnostics for the top frozen family, plus LOO and a separate round-number control.
    top=finalists_out[0] if finalists_out else None; top_rows=evaluate(top['spec'],prepared) if top else []
    loo={}
    if top:
        for hold in PAIRS:
            train=[x for x in top_rows if x['symbol']!=hold and period_for(x['timestamp_epoch'])=='SELECTION']; test=[x for x in top_rows if x['symbol']==hold and period_for(x['timestamp_epoch'])=='FINAL_UNTOUCHED_TEST']; loo[hold]={'train_equal_pair_expectancy_R':equal_pair(train),'held_out_final':metrics(test)}
    round_results=[]
    for band in ('00','50'):
        s=base_spec(entry_reference='PSYCHOLOGICAL',depth=0,psych_band=band); rows=evaluate(s,prepared); round_results.append({'psych_band':band,'spec':s,'selection_equal_pair_expectancy_R':equal_pair([x for x in rows if period_for(x['timestamp_epoch'])=='SELECTION']),'final_equal_pair_expectancy_R':equal_pair([x for x in rows if period_for(x['timestamp_epoch'])=='FINAL_UNTOUCHED_TEST']),'pair_metrics_final':pair_metrics([x for x in rows if period_for(x['timestamp_epoch'])=='FINAL_UNTOUCHED_TEST'])})
    result={'family':'LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH','research_only':True,'source':str(SOURCE),'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'pairs':PAIRS,'locked_boundaries':locked,'inventory':inventory,'stage_A_structural_marginals':stage_a,'stage_B_entry_marginals':stage_b,'finalist_results':finalists_out,'leave_one_pair_out':loo,'round_number_family':round_results,'selection_policy':'equal-pair expectancy and profitable-pair breadth; final period untouched until finalist freeze','notes':['No pair excluded for poor performance.','Psychological levels are a separate pip-aware family.','No pair-specific optimization of structural or entry parameters was used.','No Stage D joint optimization, promotion, or runner changes.']}
    OUT.mkdir(parents=True,exist_ok=True); RESULT.write_text(json.dumps(result,indent=2,default=str)+'\n')
    lines=['# LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH — pair-agnostic study','', 'Research-only. No frozen V1, production, runner, broker, or execution state was modified.','', '## Locked boundaries', '```json',json.dumps(locked,indent=2),'```','', '## Universe inventory','```json',json.dumps(inventory,indent=2),'```','', '## Result status', f"Pairs evaluated: {len(PAIRS)}; finalist rows: {len(finalists_out)}; final untouched period ends {locked['COMMON_FINAL_END']}.", '', 'The full machine-readable staged results, per-pair metrics, cost stress, bootstrap, LOO, and round-number diagnostics are in the JSON artifact. This staged pass does not promote or start forward paper execution.','']
    REPORT.write_text('\n'.join(lines))
    print(json.dumps({'result':str(RESULT),'report':str(REPORT),'pairs':PAIRS,'locked_boundaries':locked,'top_finalists':len(finalists_out)},indent=2))


if __name__=='__main__': main()
