"""Offline synthetic or explicitly developmental Donchian V1/V2 replay.

Consumes files only; no bridge, registration, publisher or execution dependency.
"""
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from core.strategies.evaluation import canonical_hash
from strategy_backtest.library.catalog import BY_KEY
from strategy_backtest.library.donchian_v2 import DEFINITION, DonchianV2Runtime
from strategy_backtest.library.runtime import ComposedResearchRuntime
from strategy_backtest.models import MarketEvent
from strategy_backtest.research_outcomes import resolve_research_entry

BASE=1800000000


def event(at,o,h,l,c,*,instrument='SYNTHETIC',tf='M15'):
    return MarketEvent(instrument,tf,at,at+(3600 if tf=='H1' else 900),o,h,l,c)


def synthetic():
    wave=(0,2,4,2,0,1)
    context=tuple(event(BASE+(i-48)*3600,80+i*.3+wave[i%6],81+i*.3+wave[i%6],
                        79+i*.3+wave[i%6],80.2+i*.3+wave[i%6],tf='H1') for i in range(48))
    context+=(event(BASE,95,96,94,95.2,tf='H1'),)
    bars=tuple(event(BASE+(i-41)*900,100,101,99,100) for i in range(40))
    bars+=tuple(event(BASE+offset,*prices) for offset,prices in (
        (-900,(101,104.5,100.5,104)), (0,(103,104,100.9,103.8)),
        (900,(103.8,105,103.4,104.5)), (1800,(104.5,106,104.2,105.8)),
        (2700,(105.8,105.9,104.8,105.0)), (3600,(105,107,104.9,106.8)),
        (4500,(106.8,107.5,106.5,107.2))))
    return bars,context


def run(bars,context,*,tick,dataset,artifact,version):
    runtime=(DonchianV2Runtime(dataset_id=dataset,artifact_digest=artifact) if version=='V2' else
             ComposedResearchRuntime(BY_KEY['donchian_breakout'],dataset_id=dataset,artifact_digest=artifact))
    counts=Counter();evaluations=[]
    for i,current in enumerate(bars):
        causal_context=tuple(x for x in context if x.close_timestamp<=current.close_timestamp)
        for record in runtime.advance(bars[:i+1],causal_context,tick_size=tick):
            counts[record.provenance['reason']]+=1
            if record.provenance.get('proposal') or record.provenance.get('research_entry'):
                evaluations.append({'evaluation_hash':canonical_hash(record.to_dict()),
                    'decision_time':record.decision_time,'candidate_id':record.candidate_id,
                    'reason':record.provenance['reason'],'proposal':record.provenance.get('proposal'),
                    'research_entry':record.provenance.get('research_entry')})
    entries=runtime.accepted_entries()
    outcomes=[asdict(resolve_research_entry(e,bars,timeframe_seconds=900)) for e in entries]
    return {'strategy_version':version,'parameter_set_id':runtime.parameter_set_id,
            'parameter_hash':runtime.parameter_hash,'dependency_hash':runtime.dependency_hash,
            'reason_counts':dict(counts),'accepted_entries':len(entries),
            'pending_entries':len(runtime.snapshot_state()['state']['pending']),
            'outcome_counts':dict(Counter(x['status'] for x in outcomes)),
            'outcomes':outcomes,'evaluations':evaluations}


def aggregated_h1(bars):
    """Require four exact M15 constituents; never bridge missing observations."""
    groups=defaultdict(list)
    for b in bars:groups[b.open_timestamp//3600*3600].append(b)
    result=[]
    for start,items in sorted(groups.items()):
        items=sorted(items,key=lambda x:x.open_timestamp)
        if [x.open_timestamp for x in items]!=list(range(start,start+3600,900)):
            continue
        result.append(event(start,items[0].open,max(x.high for x in items),min(x.low for x in items),
                            items[-1].close,instrument=items[0].canonical_instrument,tf='H1'))
    return tuple(result)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--audit-report',type=Path,help='Known development evidence only; never validation')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    paths=['strategy_backtest/library/donchian_v2.py','strategy_backtest/library/breakout_retest.py',
           'strategy_backtest/library/runtime.py','strategy_backtest/library/primitives.py',
           'strategy_backtest/research_outcomes.py','research/replay_donchian_v2.py']
    code={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths}
    artifact=canonical_hash(code)
    report={'schema':'donchian-v2-development-replay.v1','research_only':True,
            'execution_eligible':False,'broker_calls':0,'artifact_digest':artifact,'code_manifest':code,
            'definition':DEFINITION.manifest(),'definition_hash':canonical_hash(DEFINITION.manifest()),
            'classification':'SYNTHETIC_NOT_PERFORMANCE_EVIDENCE','streams':{}}
    if args.audit_report:
        raw=args.audit_report.read_bytes();data=json.loads(raw)
        if data.get('audit_version')!='donchian-evidence-audit.v1':
            raise ValueError('explicit deep audit format required')
        dataset=hashlib.sha256(raw).hexdigest()
        report.update(classification='DEVELOPMENT_ONLY_NOT_VALIDATION',dataset_sha256=dataset,
                      context_source='H1_AGGREGATED_FROM_FOUR_COMPLETED_M15_NOT_NATIVE_H1',
                      limitation='History is bounded by the supplied audit. Replayed V1 is not a reconciliation of the original forward run.')
        inputs=[]
        for symbol,stream in data['streams'].items():
            if stream.get('status')!='AVAILABLE' or stream.get('conflict_times'):
                report['streams'][symbol]={'status':'BLOCKED_REFERENCE_STREAM'};continue
            members=[x for x in data['signals'] if x.get('source_provenance',{}).get('provider_symbol')==symbol]
            ticks={(x.get('source_proposal_event') or {}).get('payload',{}).get('provenance',{}).get('proposal',{}).get('evidence',{}).get('tick_size') for x in members}
            if len(ticks)!=1 or None in ticks:
                report['streams'][symbol]={'status':'BLOCKED_FROZEN_TICK_IDENTITY'};continue
            values=stream.get('reference_candles') or []
            if not 0<len(values)<=20000:raise ValueError('bounded reference candles required')
            bars=tuple(event(x['time'],x['open'],x['high'],x['low'],x['close'],instrument=members[0]['instrument']) for x in values)
            inputs.append((symbol,bars,aggregated_h1(bars),next(iter(ticks))))
    else:
        dataset='synthetic-donchian-v2-v1';bars,context=synthetic()
        inputs=[('SYNTHETIC',bars,context,.01)]
    for symbol,bars,context,tick in inputs:
        report['streams'][symbol]={version:run(bars,context,tick=tick,dataset=dataset,artifact=artifact,version=version)
                                   for version in ('V1','V2')}
    print(json.dumps(report,indent=2,allow_nan=False))


if __name__=='__main__':main()
