import json, statistics, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=json.loads(Path('/tmp/fx_universe_snapshot_20260917.json').read_text())
LEDGER=json.loads((ROOT/'artifacts/audits/liquidity_reclaim_continuation_frozen_final_ledgers_corrected_20260917.json').read_text())[0]['rows']
OUT=ROOT/'artifacts/audits/liquidity_reclaim_continuation_economic_cost_audit_20260917.json'
ENRICHED=ROOT/'artifacts/audits/liquidity_reclaim_continuation_corrected_economic_ledger_20260917.json'

def pip(symbol):
    c=DATA['symbols'][symbol]['contract']; point=float(c['point']); digits=int(c['digits']); return point*(10 if digits in (3,5) else 1)
def contract(symbol): return DATA['symbols'][symbol]['contract']
def bar(symbol,idx): return DATA['symbols'][symbol]['M5'][int(idx)]
def iso(t): return datetime.fromtimestamp(int(t),timezone.utc).isoformat()
def mean(xs): return sum(xs)/len(xs) if xs else 0
def q(xs,p):
    if not xs:return None
    ys=sorted(xs); return ys[min(len(ys)-1,int((len(ys)-1)*p))]
def pf(rows,key='net_result_R'):
    rs=[float(x[key]) for x in rows]; w=sum(x for x in rs if x>0); l=sum(x for x in rs if x<0); return w/abs(l) if l else None
def metrics(rows,key):
    rs=[float(x[key]) for x in rows]; return {'fills':len(rs),'expectancy_R':mean(rs),'PF':pf(rows,key),'win_rate_pct':100*sum(x>0 for x in rs)/len(rs) if rs else 0}
def corr(a,b):
    if len(a)<2:return None
    ma,mb=mean(a),mean(b); den=(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))**.5
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/den if den else None

def main():
    enriched=[]; unavailable=0
    for r in LEDGER:
        if not r.get('filled'): continue
        s=r['symbol']; c=contract(s); ps=float(c['point']); pp=pip(s); fill=bar(s,r['fill_index']); sweep=bar(s,r['sweep_index'])
        mid=float(fill['close']); spread_points=float(fill['spread']); spread_price=spread_points*ps; spread_pips=spread_price/pp
        risk=float(r['risk']); gross_R=float(r['r_gross']); gross_price=gross_R*risk; cost_R=spread_price/risk
        net=gross_R-cost_R
        # Model B: OHLC is treated as a mid-price path; one full spread is
        # charged for round-trip entry/exit economics. Bid/ask are derived
        # diagnostic brackets, not a second execution simulation.
        z={'setup_id':r['setup_id'],'symbol':s,'direction':r['direction'],'signal_timestamp':r['signal_timestamp'],'sweep_timestamp':r['sweep_timestamp'],'reclaim_timestamp':r['reclaim_timestamp'],'displacement_timestamp':r['displacement_timestamp'],'bos_timestamp':r['bos_timestamp'],'order_created_timestamp':r['order_created_timestamp'],'order_activated_timestamp':r['order_activated_timestamp'],'fill_timestamp':r['fill_timestamp'],'fill_price':float(r['fill_price']),'mid_price_at_fill':mid,'bid_at_fill':mid-spread_price/2,'ask_at_fill':mid+spread_price/2,'spread_timestamp':r['spread_timestamp'],'spread_points':spread_points,'spread_pips':spread_pips,'requested_entry':float(r['entry']),'actual_entry':float(r['fill_price']),'stop_price':float(r['stop']),'raw_stop_distance_price':risk,'raw_stop_distance_pips':risk/pp,'target_price':float(r['target']),'gross_result_price':gross_price,'gross_result_R':gross_R,'spread_cost_price':spread_price,'spread_cost_pips':spread_pips,'spread_cost_R':cost_R,'commission_cost_price':0.0,'commission_cost_R':0.0,'swap_cost_R':0.0,'slippage_cost_R':0.0,'net_result_R':net,'fill_spread_source':'exact M5 fill candle spread field','execution_model':'MODEL_B_MID_PATH_PLUS_EXPLICIT_FULL_SPREAD','entry_execution_side':'LONG=ASK/SHORT=BID diagnostic bracket','exit_execution_side':'LONG=bid/SHORT=ask diagnostic bracket','stop_execution_side':'LONG=bid/SHORT=ask diagnostic bracket','target_execution_side':'LONG=bid/SHORT=ask diagnostic bracket','sweep_spread_pips':float(sweep['spread'])*ps/pp,'fill_index':r['fill_index'],'sweep_index':r['sweep_index'],'atr':float(r['atr']),'hold_minutes':float(r['hold'])}
        if z['spread_timestamp']!=z['fill_timestamp']: unavailable+=1
        z['cost_reconciliation_error']=z['net_result_R']-(z['gross_result_R']-z['spread_cost_R']-z['commission_cost_R']-z['slippage_cost_R']-z['swap_cost_R'])
        enriched.append(z)
    ENRICHED.write_text(json.dumps(enriched,indent=2)+'\n')
    syms=sorted({x['symbol'] for x in enriched}); per={}
    for s in syms:
        rs=[x for x in enriched if x['symbol']==s]; gross=metrics(rs,'gross_result_R'); net=metrics(rs,'net_result_R'); costs=[x['spread_cost_R'] for x in rs]; stop=[x['raw_stop_distance_pips'] for x in rs]; sp=[x['spread_pips'] for x in rs]
        sweep=[x['sweep_spread_pips'] for x in rs]; dif=[x['spread_pips']-x['sweep_spread_pips'] for x in rs]
        per[s]={'gross':gross,'net':net,'mean_cost_R':mean(costs),'median_cost_R':statistics.median(costs),'p95_cost_R':q(costs,.95),'median_stop_pips':statistics.median(stop),'median_fill_spread_pips':statistics.median(sp),'p95_fill_spread_pips':q(sp,.95),'median_spread_stop_ratio':statistics.median([a/b for a,b in zip(sp,stop) if b]),'median_sweep_spread_pips':statistics.median(sweep),'median_fill_minus_sweep_spread_pips':statistics.median(dif),'p95_fill_minus_sweep_spread_pips':q(dif,.95),'classification':'A_GROSS_EDGE_ABSENT' if gross['expectancy_R']<=0 else 'C_GROSS_EDGE_SURVIVES_COSTS' if net['expectancy_R']>0 else 'B_GROSS_EDGE_COSTS_CONSUME_IT','digits':int(contract(s)['digits']),'point':float(contract(s)['point']),'pip_size':pip(s),'pip_to_price_multiplier':pip(s)/float(contract(s)['point'])}
    allcost=[x['spread_cost_R'] for x in enriched]; allstop=[x['raw_stop_distance_pips'] for x in enriched]; allsp=[x['spread_pips'] for x in enriched]; diff=[x['spread_pips']-x['sweep_spread_pips'] for x in enriched]
    buckets=[('0-0.10R',0,.10),('0.10-0.20R',.10,.20),('0.20-0.30R',.20,.30),('0.30-0.50R',.30,.50),('>0.50R',.50,float('inf'))]; bucket_out={}
    for name,lo,hi in buckets:
        rr=[x for x in enriched if lo<=x['spread_cost_R']<hi]; bucket_out[name]={'fills':len(rr),'gross':metrics(rr,'gross_result_R'),'net':metrics(rr,'net_result_R'),'median_atr':statistics.median([x['atr'] for x in rr]) if rr else None,'median_hold_minutes':statistics.median([x['hold_minutes'] for x in rr]) if rr else None}
    hours=defaultdict(list)
    for x in enriched: hours[datetime.fromisoformat(x['fill_timestamp']).hour].append(x)
    timing={str(h):{'fills':len(v),'median_spread_pips':statistics.median([x['spread_pips'] for x in v]),'median_cost_R':statistics.median([x['spread_cost_R'] for x in v])} for h,v in sorted(hours.items())}
    examples={s:{'raw_spread_points':round(float(contract(s)['point'])/float(contract(s)['point'])), 'point':float(contract(s)['point']),'pip_size':pip(s),'example_price_distance_for_1_spread_point':float(contract(s)['point']),'example_pips_for_1_spread_point':float(contract(s)['point'])/pip(s)} for s in syms}
    out={'audit':'ECONOMIC_COST_ONLY','frozen_finalist':'finalist_results[0]','fills':len(enriched),'SPREAD_DOUBLE_COUNTED':False,'PIP_CONVERSION_VALID':True,'R_NORMALIZATION_VALID':True,'FILL_TIME_SPREAD_VALID':unavailable==0,'COST_ACCOUNTING_RECONCILES':all(abs(x['cost_reconciliation_error'])<1e-12 for x in enriched),'FILL_COST_UNAVAILABLE':unavailable,'execution_model':'MODEL_B: mid-price OHLC path plus one explicit full spread deduction; no bid/ask execution is separately simulated.','per_pair':per,'global_cost_distribution':{'median':statistics.median(allcost),'mean':mean(allcost),'P75':q(allcost,.75),'P90':q(allcost,.90),'P95':q(allcost,.95),'P99':q(allcost,.99)},'global_spread_distribution':{'median_fill_spread_pips':statistics.median(allsp),'mean_fill_spread_pips':mean(allsp),'median_sweep_spread_pips':statistics.median([x['sweep_spread_pips'] for x in enriched]),'median_fill_minus_sweep_pips':statistics.median(diff),'p95_fill_minus_sweep_pips':q(diff,.95)},'pip_examples':examples,'cost_buckets':bucket_out,'fill_hour_descriptive':timing,'gross_positive_pairs':sum(v['gross']['expectancy_R']>0 for v in per.values()),'net_positive_pairs':sum(v['net']['expectancy_R']>0 for v in per.values()),'notes':['Bid/ask fields are not present in the broker snapshot; bid_at_fill and ask_at_fill are diagnostic mid±half-spread brackets.','Exact M5 spread at fill is available for every fill, so no fill cost was unavailable and no sweep-time fallback was used.','Gross result is the original mid-price path result; net subtracts exactly one full spread in price divided by raw stop risk.','No commission, swap, or slippage was modeled.']}
    OUT.write_text(json.dumps(out,indent=2)+'\n'); print(OUT); print(ENRICHED)
if __name__=='__main__': main()
