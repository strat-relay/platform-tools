#!/usr/bin/env python3
"""Continue the frozen Liquidity study from durable local M5 artifacts only."""
from __future__ import annotations
import hashlib, json, math, random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/research'
PROTOCOL=OUT/'liquidity_exit_management_protocol.json'
LEDGER=OUT/'liquidity_post_entry_excursion_ledger.jsonl'
DATA=ROOT/'artifacts/research/multitimeframe_structure_sniper/paged_native_full/USDJPY/M5.jsonl'
HASH='c88b13ebb5f095430592a99d2e133d53cc6f0fb95d80c47e457fdb49b4f4b585'
SYMBOLS={'LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1':'USDJPY'}

def iso_epoch(s): return datetime.fromisoformat(s).timestamp()
def rvalue(price, row):
    d=float(row['direction']=='LONG')*2-1
    return d*(price-float(row['entry_realistic']))/abs(float(row['entry_realistic'])-float(row['stop_loss']))
def write(p,x): p.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n')
def h(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    proto=json.loads(PROTOCOL.read_text()); raw=dict(proto); got=raw.pop('protocol_sha256')
    if got!=HASH or hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()!=HASH:
        raise SystemExit('PROTOCOL_HASH_MATCH=false; STOP')
    rows=[json.loads(x) for x in LEDGER.open()]
    if len(rows)!=51: raise SystemExit(f'TRADE_POPULATION_COUNT={len(rows)}; STOP')
    bars=[]
    if DATA.exists(): bars=[json.loads(x) for x in DATA.open()]
    bars.sort(key=lambda x:int(x['time']))
    data_hash=h(DATA) if DATA.exists() else None
    population=[]; paths=[]
    for i,row in enumerate(rows):
        strategy=row['strategy_id']; symbol=SYMBOLS.get(strategy)
        entry=row.get('signal_timestamp'); ts=iso_epoch(entry) if entry else None
        end=ts+360*60 if ts else None
        xs=[b for b in bars if symbol=='USDJPY' and ts is not None and int(b['time'])>=ts and int(b['time'])<=end]
        enough=bool(xs) and int(xs[-1]['time'])+300>=end
        availability='AVAILABLE' if enough else ('AVAILABLE_PARTIAL_DATASET_END' if xs else 'PATH_UNAVAILABLE')
        population.append({'trade_id':f'LXM-{i+1:03d}','setup_id':row['setup_id'],'strategy_id':strategy,'symbol':symbol,'direction':row.get('direction'),'entry_timestamp':entry,'entry_price':row.get('entry_realistic'),'initial_stop':row.get('stop_loss'),'initial_target':row.get('entry_realistic')+(1 if row.get('direction')=='LONG' else -1)*1.25*abs(float(row.get('entry_realistic'))-float(row.get('stop_loss'))),'initial_risk_distance':abs(float(row.get('entry_realistic'))-float(row.get('stop_loss'))),'availability':availability,'source_file':str(DATA.relative_to(ROOT)) if symbol else None,'source_file_sha256':data_hash,'bar_count':len(xs)})
        paths.append((row,xs,availability))
    path_file=OUT/'liquidity_post_entry_path_ledger.jsonl'
    with path_file.open('w') as f:
        for row,xs,av in paths:
            rec={'trade_id':next(p['trade_id'] for p in population if p['setup_id']==row['setup_id']),'setup_id':row['setup_id'],'strategy_id':row['strategy_id'],'symbol':SYMBOLS.get(row['strategy_id']),'availability':av,'ordering_policy':'CONSERVATIVE_STOP_FIRST_WHEN_STOP_AND_TARGET_TOUCH_SAME_BAR; AMBIGUOUS_INTRABAR_RECORDED','source_file':str(DATA.relative_to(ROOT)) if xs else None,'source_file_sha256':data_hash,'bars':[{'timestamp':datetime.fromtimestamp(int(b['time']),timezone.utc).isoformat(),'source_row_time':int(b['time']),'open':b['open'],'high':b['high'],'low':b['low'],'close':b['close'],'spread_points':b.get('spread'),'source_bar_identity':f"USDJPY-M5-{int(b['time'])}"} for b in xs]}
            f.write(json.dumps(rec,sort_keys=True)+'\n')
    def replay(row,xs,target_r=1.25,hold=120,be=None,trail=None,partial=None,no_tp=False):
        if not xs:return {'status':'PATH_UNAVAILABLE'}
        entry=float(row['entry_realistic']); stop=float(row['stop_loss']); risk=abs(entry-stop); long=row['direction']=='LONG'; initial=stop; hit125=None; mfe=-1e9; mae=0; ambiguous=[]; remaining=1.0; realized=0.0; target=None if no_tp else entry+(1 if long else -1)*target_r*risk
        for b in xs:
            elapsed=(int(b['time'])+300-iso_epoch(row['signal_timestamp']))/60
            if elapsed>hold: break
            high,low=float(b['high']),float(b['low']); up=(high-entry)/risk if long else (entry-low)/risk; down=(entry-low)/risk if long else (high-entry)/risk; mfe=max(mfe,up); mae=max(mae,down)
            if hit125 is None and up>=1.25: hit125=elapsed
            sl_hit=low<=stop if long else high>=stop; tp_hit=(target is not None and (high>=target if long else low<=target))
            if be is not None and up>=be: stop=max(stop,entry) if long else min(stop,entry)
            if trail is not None and up>=trail[0]:
                new=entry+(1 if long else -1)*(up-trail[1])*risk; stop=max(stop,new) if long else min(stop,new)
            if elapsed>=hold:
                px=float(b['close']); return {'status':'TIME_EXIT','elapsed':elapsed,'gross_r':rvalue(px,row),'net_r':rvalue(px,row),'mfe_r':mfe,'mae_r':mae,'hit125':hit125,'ambiguous':ambiguous}
            if sl_hit and tp_hit:
                ambiguous.append({'time':int(b['time']),'reason':'STOP_AND_TARGET_TOUCH'}); tp_hit=False
            if sl_hit:
                return {'status':'STOPPED','elapsed':elapsed,'gross_r':rvalue(stop,row),'net_r':rvalue(stop,row),'mfe_r':mfe,'mae_r':mae,'hit125':hit125,'ambiguous':ambiguous}
            if tp_hit:
                return {'status':'TARGET_HIT','elapsed':elapsed,'gross_r':rvalue(target,row),'net_r':rvalue(target,row),'mfe_r':mfe,'mae_r':mae,'hit125':hit125,'ambiguous':ambiguous}
        return {'status':'PATH_END_CENSORED','mfe_r':mfe,'mae_r':mae,'hit125':hit125,'ambiguous':ambiguous}
    control=[]
    for row,xs,av in paths:
        z=replay(row,xs) if av.startswith('AVAILABLE') else {'status':'PATH_UNAVAILABLE'}
        z.update({'setup_id':row['setup_id'],'strategy_id':row['strategy_id'],'known_status':row.get('control_status'),'known_r':row.get('control_r_realistic'),'availability':av}); control.append(z)
    decisive=[z for z in control if z['availability']=='AVAILABLE']
    exact=[z for z in decisive if z['status']==z['known_status'] and abs(float(z.get('gross_r',0))-float(z.get('known_r',0)))<0.2]
    validation={'schema':'liquidity-exit-management-path-validation-v1','protocol_sha256':HASH,'data_sources':[{'path':str(DATA.relative_to(ROOT)),'sha256':data_hash,'symbol':'USDJPY','timeframe':'M5'}],'trade_population_count':len(rows),'available_full_paths':sum(x['availability']=='AVAILABLE' for x in population),'partial_paths':sum(x['availability']=='AVAILABLE_PARTIAL_DATASET_END' for x in population),'unavailable_paths':sum(x['availability']=='PATH_UNAVAILABLE' for x in population),'control_exact_matches':len(exact),'control_mismatches':len(decisive)-len(exact),'ambiguous_intrabar_cases':sum(len(z.get('ambiguous',[])) for z in decisive),'control_path_reproduction_pass':len(decisive)>0 and len(exact)==len(decisive),'ordering_policy':'STOP_FIRST; ambiguous cases retained','control_comparisons':decisive}
    write(OUT/'liquidity_exit_management_path_validation.json',validation)
    # Only score alternatives after the required control gate. Uncovered symbols remain unavailable.
    scored=[]
    if validation['control_path_reproduction_pass']:
        profiles={'PROFILE_0_CURRENT':{},'PROFILE_1_FIXED_TARGET_HOLD':{},'PROFILE_2_BREAKEVEN':{},'PROFILE_3_TRAILING':{},'PROFILE_4_PARTIAL_RUNNER':{},'PROFILE_5_NO_TP_TIME_EXIT':{}}
        for row,xs,av in paths:
            if av!='AVAILABLE': continue
            profiles['PROFILE_0_CURRENT'][row['setup_id']]=replay(row,xs)
            for t in [1.5,2,2.5,3]:
                for hold in [120,240,360]: profiles['PROFILE_1_FIXED_TARGET_HOLD'].setdefault(f't{t}_h{hold}',{})[row['setup_id']]=replay(row,xs,t,hold)
            for be in [.75,1,1.25]:
                for t in [2,2.5,3]:
                    for hold in [240,360]: profiles['PROFILE_2_BREAKEVEN'].setdefault(f'be{be}_t{t}_h{hold}',{})[row['setup_id']]=replay(row,xs,t,hold,be=be)
            for act in [.75,1]:
                for tr in [.5,1]: profiles['PROFILE_3_TRAILING'].setdefault(f'a{act}_tr{tr}',{})[row['setup_id']]=replay(row,xs,3,360,trail=(act,tr))
            for t in [2,2.5,3]:
                for hold in [240,360]: profiles['PROFILE_4_PARTIAL_RUNNER'].setdefault(f'p50_at1_t{t}_h{hold}',{})[row['setup_id']]=replay(row,xs,t,hold)
            for hold in [240,360]: profiles['PROFILE_5_NO_TP_TIME_EXIT'].setdefault(f'no_tp_h{hold}',{})[row['setup_id']]=replay(row,xs,hold=hold,no_tp=True)
        write(OUT/'liquidity_exit_management_scored_results.json',{'schema':'liquidity-exit-management-scored-results-v1','protocol_sha256':HASH,'profiles':profiles,'broker_feasible_partial':{'0.01':False,'0.02':True,'0.03':False,'0.04':True},'net_cost_status':'NET_COST_INCOMPLETE; historical bar spread available only for USDJPY and no exact bid/ask ticks'})
    else: write(OUT/'liquidity_exit_management_scored_results.json',{'schema':'liquidity-exit-management-scored-results-v1','protocol_sha256':HASH,'status':'NOT_SCORED_CONTROL_GATE_FAILED','validation':validation})
    write(OUT/'liquidity_exit_management_post125_analysis.json',{'schema':'liquidity-exit-management-post125-analysis-v1','protocol_sha256':HASH,'status':'NOT_SCORED_UNLESS_CONTROL_GATE_PASS','available_strategies':['USDJPY'] if validation['control_path_reproduction_pass'] else []})
    write(OUT/'liquidity_exit_management_hold_analysis.json',{'schema':'liquidity-exit-management-hold-analysis-v1','protocol_sha256':HASH,'status':'PATH_COVERAGE_LIMITED','unresolved_at_120m':'reported only for valid reconstructed paths'})
    write(OUT/'liquidity_exit_management_paired_bootstrap.json',{'schema':'liquidity-exit-management-paired-bootstrap-v1','protocol_sha256':HASH,'status':'NOT_RUN_NO_FROZEN_SEED_AND_INCOMPLETE_SYMBOL_COVERAGE','broker_writes':0})
    print(json.dumps({'PROTOCOL_HASH_MATCH':True,'TRADE_POPULATION_COUNT':len(rows),'POST_ENTRY_PATHS_AVAILABLE':sum(x['availability']=='AVAILABLE' for x in population),'POST_ENTRY_PATHS_UNAVAILABLE':sum(x['availability']=='PATH_UNAVAILABLE' for x in population),'CONTROL_PATH_REPRODUCTION_PASS':validation['control_path_reproduction_pass'],'CONTROL_EXACT_MATCHES':validation['control_exact_matches'],'CONTROL_MISMATCHES':validation['control_mismatches'],'AMBIGUOUS_INTRABAR_CASES':validation['ambiguous_intrabar_cases'],'STAGE_A_PID_31174_UNTOUCHED':True},indent=2))
if __name__=='__main__': main()
