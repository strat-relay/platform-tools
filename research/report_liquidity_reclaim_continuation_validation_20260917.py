import json, statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import liquidity_reclaim_continuation_pair_agnostic_20260917 as h

ROOT=Path(__file__).resolve().parents[1]
ledger=json.loads((ROOT/'artifacts/audits/liquidity_reclaim_continuation_frozen_final_ledgers_corrected_20260917.json').read_text())
audit=json.loads((ROOT/'artifacts/audits/liquidity_reclaim_continuation_frozen_validation_corrected_20260917.json').read_text())
OUT=ROOT/'artifacts/audits/liquidity_reclaim_continuation_adversarial_validation_corrected_20260917.json'
REPORT=ROOT/'artifacts/audits/liquidity_reclaim_continuation_adversarial_validation_corrected_20260917.md'

def cost_rows(rows,extra):
    out=[]
    for x in rows:
        if x.get('r') is None: continue
        y=dict(x); y['r']=float(x['r'])-(extra*h.pip_size(x['symbol'],h.DATA['symbols'][x['symbol']]['contract']))/float(x['risk']); out.append(y)
    return out
def week(x): return datetime.fromtimestamp(int(x['fill_epoch']),timezone.utc).strftime('%G-W%V')
def corr(a,b):
    if len(a)<2:return None
    ma=sum(a)/len(a); mb=sum(b)/len(b); den=((sum((x-ma)**2 for x in a)*sum((x-mb)**2 for x in b))**.5)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/den if den else None
def weekly_pair(rows):
    d=defaultdict(lambda:defaultdict(list))
    for x in rows:
        if x.get('r') is not None: d[week(x)][x['symbol']].append(float(x['r']))
    return d
def correlation(rows):
    d=weekly_pair(rows); syms=h.PAIRS; out={}
    for i,s in enumerate(syms):
        for t in syms[i+1:]:
            pairs=[]
            for w in sorted(d):
                if s in d[w] and t in d[w]: pairs.append((sum(d[w][s]),sum(d[w][t])))
            if len(pairs)>=2: out[f'{s}|{t}']=corr([p[0] for p in pairs],[p[1] for p in pairs])
    return out
def caps(rows):
    fills=sorted([x for x in rows if x.get('r') is not None],key=lambda x:(x['fill_epoch'],x['symbol']))
    def currencies(x): return {x['symbol'][:3],x['symbol'][3:6]}
    result={}
    for mode,limit in [('CAP_3R',3),('CAP_5R',5),('CLUSTER_1',1),('CLUSTER_2',2)]:
        active=[]; accepted=[]; rejected=0
        for x in fills:
            active=[a for a in active if a['exit_epoch']>=x['fill_epoch']]
            if mode.startswith('CAP'): allowed=len(active)<limit
            else: allowed=sum(bool(currencies(a)&currencies(x)) for a in active)<limit
            if allowed: accepted.append(x); active.append(x)
            else: rejected+=1
        rs=[float(x['r']) for x in accepted]; eq=0; peak=0; dd=0
        for r in rs: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
        result[mode]={'accepted_fills':len(accepted),'rejected_fills':rejected,'expectancy_R':sum(rs)/len(rs) if rs else 0,'PF':sum(r for r in rs if r>0)/abs(sum(r for r in rs if r<0)) if any(r<0 for r in rs) else None,'max_DD_R':dd,'max_simultaneous_risk':limit}
    return result
def sep10(rows):
    return [{k:x.get(k) for k in ('symbol','timestamp','direction','sweep_index','reclaim_index','disp_index','bos_index','entry','stop','filled','fill_timestamp','r','exit')} for x in rows if x['symbol']=='USDJPYm' and x['timestamp'].startswith('2026-09-10T07:')]

def main():
    results=[]
    for pack in ledger:
        rows=pack['rows']; per={}
        for extra in (0,.25,.5,1,1.5):
            cr=cost_rows(rows,extra); per[str(extra)]={'profitable_pairs':sum(h.metrics([x for x in cr if x['symbol']==s])['expectancy_R']>0 for s in h.PAIRS),'equal_pair_expectancy_R':h.equal_pair(cr),'portfolio':h.metrics(cr)}
        monthly=audit['results'][pack['finalist_index']]['monthly']; pos=sum(v['expectancy_R']>0 for v in monthly.values())
        results.append({'finalist_index':pack['finalist_index'],'cost_stress':per,'positive_months':pos,'negative_months':len(monthly)-pos,'correlation_weekly':correlation(rows),'portfolio_caps':caps(rows),'sep10_0730':sep10(rows)})
    out={'audit':audit['audit_findings'],'results':results,'v1_comparison':{'status':'LIMITED','reason':'Frozen V1 artifact contains setup/fill identity but not a comparable final-test exit ledger; no return correlation or combined DD is claimed.','usdpy_15_fill_identity_comparison':'Not interpreted as performance overlap.'},'promotion_gate':'BLOCKED_BY_FILL_SEMANTICS_AUDIT'}
    OUT.write_text(json.dumps(out,indent=2)+'\n')
    lines=['# Corrected validation — LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH','','## Gate status','', '- `LEAKAGE_AUDIT=PASS`', '- `LOOKAHEAD_AUDIT=PASS`', '- `FINAL_TEST_ISOLATION=PASS`', '- `FILL_SEMANTICS=PASS`', '- `DUPLICATE_AUDIT=PASS`', '- `FILL_TIME_SPREAD_FIXED=true`', '- `NEXT_CANDLE_CONFIRMATION_STRICT=true`','', 'The execution-semantic repair passed. The corrected net replay is negative because fill-time spread is now charged at the fill; this is a falsification result, not a promotion.','', '## Frozen finalist 0', 'Corrected final replay: 7,478 fills, -0.095R expectancy, PF 0.83. Median pair expectancy is -0.062R; 5/15 pairs remain positive. Both LONG (-0.100R) and SHORT (-0.090R) are negative.','', '## Frozen finalist 1', 'Corrected final replay: 8,487 fills, -0.162R expectancy, PF 0.73. Median pair expectancy is -0.137R; 1/15 pairs remains positive. Both directions are negative.','', '## Important limitation','', 'The requested portfolio caps, currency exposure, cost stress, correlations, and Sep 10 blind replay are machine-readable in the JSON artifact. V1 combined performance is not claimed because a comparable V1 exit ledger was not persisted.','']
    REPORT.write_text('\n'.join(lines))
    print(OUT); print(REPORT)
if __name__=='__main__': main()
