"""Offline catalog export and reproducible strategy-library replay.

Examples:
 python -m research.strategy_library catalog --output docs/strategy_library/catalog.json
 python -m research.strategy_library synthetic --output docs/strategy_library/synthetic_report.json
 python -m research.strategy_library replay --key donchian_breakout --dataset /path/audited.json --output /path/report.json
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from core.strategies.evaluation import canonical_hash
from strategy_backtest.models import MarketEvent
from strategy_backtest.library import DEFINITIONS, EXISTING, BY_KEY, ComposedResearchRuntime
from strategy_backtest.library.runtime import STEPS, ENGINE, validate_series
from strategy_backtest.library.primitives import VERSION
from strategy_backtest.research_outcomes import resolve_research_entry, POLICY_VERSION, RESOLVER_VERSION


def artifact_identity():
    root=Path(__file__).resolve().parents[1]
    paths=list((root/'strategy_backtest/library').glob('*.py'))+[
        root/'strategy_backtest/models.py',root/'strategy_backtest/research_outcomes.py',
        root/'core/strategies/evaluation/models.py',root/'core/strategies/entry_timing.py',
        root/'core/strategies/entry_quote.py',root/'research/library_event_consumer.py',
        root/'research/library_event_store.py',Path(__file__).resolve(),
        root/'research/strategy_library_fixtures.py']
    manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return canonical_hash(manifest),manifest


def catalog():
    artifact,files=artifact_identity()
    return {'schema':'standard-strategy-library.v1','status':'RESEARCH_IMPLEMENTATION_NOT_VALIDATION',
            'definitions':[d.manifest()|{'strategy_id':d.strategy_id,'parameter_hash':canonical_hash(d.manifest()),
                'primitive_version':VERSION,'artifact_digest':artifact,'historical_validation':'NOT_RUN'} for d in DEFINITIONS],
            'existing_identity_bindings':EXISTING,'additional_existing_preserved':[
                'CONTEXT_STRUCTURE_RETRACE_V1','CONTEXT_STRUCTURE_RETRACE_V2',
                'KOJO_STRUCTURE_RECLAIM_V1','KOJO_STRUCTURE_RECLAIM_V2','KOJO_STRUCTURE_RECLAIM_V3'],
            'artifact_digest':artifact,'source_files':files,'runtime':ENGINE,
            'outcome_policy':POLICY_VERSION,'resolver':RESOLVER_VERSION,
            'family_counts':dict(Counter([d.family for d in DEFINITIONS]+[e['family'] for e in EXISTING])),
            'registration_performed':False,'execution_enabled':False}


def _summarize(runtime,records,bars,step,expected_gaps=()):
    entries=runtime.accepted_entries()
    outcomes=[resolve_research_entry(e,bars,timeframe_seconds=step,expected_gaps=expected_gaps).to_dict() for e in entries]
    reasons=Counter(r.provenance['reason'] for r in records)
    cancellations=sum(v for k,v in reasons.items() if k in {'MISSING_NEXT_CANDLE','ENTRY_WINDOW_EXPIRED',
                      'ENTRY_GAP_CANCELLED','INVALID_ENTRY_GEOMETRY','ENTRY_RR_BELOW_MINIMUM'})
    pending=len(runtime.snapshot_state()['state']['pending'])
    return {'evaluations':len(records),'reasons':dict(reasons),'proposals':reasons['PROPOSAL'],
            'accepted_reconstructed_entries':len(entries),'cancellations':cancellations,'pending':pending,
            'counts_reconcile':reasons['PROPOSAL']==len(entries)+cancellations+pending,
            'entries':[asdict(e) for e in entries],'outcomes':outcomes,
            'outcome_counts':dict(Counter(o['status'] for o in outcomes)),
            'valuation':'GROSS_REFERENCE_PRICE_NO_BROKER_COSTS',
            'records':[r.to_dict() for r in records]}


def synthetic():
    from research.strategy_library_fixtures import snapshot
    artifact,_=artifact_identity();reports=[]
    for d in DEFINITIONS:
        for direction in ('LONG','SHORT'):
            bars,context=snapshot(d)
            if direction=='SHORT':
                def mirror(b):return replace(b,open=200-b.open,high=200-b.low,low=200-b.high,close=200-b.close)
                bars=tuple(mirror(b) for b in bars);context=tuple(mirror(b) for b in context)
            entry_open=bars[-1].close_timestamp;step=STEPS[d.execution];price=bars[-1].close
            entry=replace(bars[-1],open_timestamp=entry_open,close_timestamp=entry_open+step,
                          open=price,high=price+.2,low=price-.2,close=price)
            boundary=entry.close_timestamp//STEPS[d.context]*STEPS[d.context]
            if context[-1].close_timestamp<boundary:
                context=context+(replace(context[-1],open_timestamp=context[-1].close_timestamp,close_timestamp=boundary),)
            dataset=canonical_hash({'bars':[asdict(b) for b in bars+(entry,)],'context':[asdict(b) for b in context]})
            runtime=ComposedResearchRuntime(d,dataset_id=dataset,artifact_digest=artifact)
            # Exclude future H1 context from the proposal snapshot even though
            # the immutable dataset identity covers the entire replay input.
            initial_context=tuple(c for c in context if c.close_timestamp<=bars[-1].close_timestamp)
            records=list(runtime.advance(bars,initial_context,tick_size=.01))
            records.extend(runtime.advance(bars+(entry,),context,tick_size=.01))
            report=_summarize(runtime,records,bars+(entry,),step)
            # Separate hand-authored barrier fixtures exercise the shared resolver.
            # These outcomes are test assertions, never empirical win/loss samples.
            barrier_fixtures={}
            for accepted in runtime.accepted_entries():
                sign=1 if accepted.direction=='LONG' else -1
                for label,level in [('TP',accepted.target_price),('SL',accepted.stop_price)]:
                    exitbar=replace(entry,open_timestamp=entry.close_timestamp,
                                    close_timestamp=entry.close_timestamp+step,
                                    open=entry.open,high=max(entry.open,level)+.001,
                                    low=min(entry.open,level)-.001,close=level)
                    resolved=resolve_research_entry(accepted,bars+(entry,exitbar),timeframe_seconds=step)
                    barrier_fixtures[label]=resolved.to_dict()
            report['synthetic_barrier_fixtures']=barrier_fixtures
            reports.append({'strategy':d.strategy_id,'direction':direction,**report})
    return {'schema':'strategy-library-synthetic.v1','evidence_class':'SYNTHETIC_FIXTURE',
            'market_performance_claims':False,'artifact_digest':artifact,
            'new_definitions':len(DEFINITIONS),'existing_bindings':len(EXISTING),
            'scenarios':len(reports),'all_counts_reconcile':all(r['counts_reconcile'] for r in reports),
            'all_have_accepted_entry':all(r['accepted_reconstructed_entries']>=1 for r in reports),
            'reports':reports}


def replay(key,dataset_path,*,definition=None,artifact_override=None):
    raw=Path(dataset_path).read_bytes();data=json.loads(raw);d=definition or BY_KEY[key]
    for required in ('source','timestamp_semantics','tick_size','bars','context','evidence_class'):
        if required not in data:raise ValueError(f'dataset missing {required}')
    if data['timestamp_semantics']!='UTC_OPEN_SECONDS':raise ValueError('normalize timestamps in a separately audited step')
    if data['evidence_class'] not in {'RETROSPECTIVE_HISTORICAL','FORWARD_RECONSTRUCTED','SYNTHETIC_FIXTURE'}:
        raise ValueError('unsupported evidence class; no automatic untouched-holdout claim')
    bars=tuple(MarketEvent(**b) for b in data['bars']);context=tuple(MarketEvent(**b) for b in data['context'])
    if not bars:raise ValueError('empty dataset')
    cutoff=bars[-1].close_timestamp;instrument=bars[-1].canonical_instrument
    validate_series(bars,d.execution,instrument,cutoff)
    validate_series(context,d.context,instrument,cutoff)
    artifact,files=artifact_override or artifact_identity();dataset=hashlib.sha256(raw).hexdigest()
    runtime=ComposedResearchRuntime(d,dataset_id=dataset,artifact_digest=artifact)
    records=[]
    # Every prefix retains the same indicator seed/history; no rolling re-seed.
    for i,b in enumerate(bars):
        c=tuple(x for x in context if x.close_timestamp<=b.close_timestamp)
        records.extend(runtime.advance(bars[:i+1],c,tick_size=data['tick_size']))
    report=_summarize(runtime,records,bars,STEPS[d.execution],data.get('expected_gaps',()))
    return {'schema':'strategy-library-replay.v1','definition':d.manifest(),'dataset_sha256':dataset,
            'artifact_digest':artifact,'source_files':files,'source':data['source'],
            'evidence_class':data['evidence_class'],'outcome_policy':POLICY_VERSION,
            'availability_assumptions':'completed-close unless data_available_at supplied; ingestion audit is external',
            'performance_validation':'EXPLORATORY_ONLY','report':report}


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('action',choices=['catalog','synthetic','replay'])
    parser.add_argument('--key',choices=list(BY_KEY));parser.add_argument('--dataset')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.action=='catalog':result=catalog()
    elif args.action=='synthetic':result=synthetic()
    else:
        if not args.key or not args.dataset:parser.error('replay requires --key and --dataset')
        result=replay(args.key,args.dataset)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n')


if __name__=='__main__':main()
