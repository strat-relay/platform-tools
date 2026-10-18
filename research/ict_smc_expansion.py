"""Opt-in frozen research versions; never registers or publishes strategies.

python -m research.ict_smc_expansion synthetic --output /tmp/ict-smc-replay.json
python -m research.ict_smc_expansion replay --ref ICT_POWER_OF_THREE@V1 --dataset /audited.json --output /tmp/report.json
"""
import argparse
from dataclasses import asdict,replace
import hashlib
import json
from pathlib import Path
from core.strategies.evaluation import canonical_hash
from strategy_backtest.library.expansion import DEFINITIONS,BY_REF
from strategy_backtest.library.runtime import ComposedResearchRuntime,STEPS
from research.strategy_library import artifact_identity as base_artifact,replay,_summarize
from research.ict_smc_fixtures import snapshot


def artifact_identity():
    _,files=base_artifact();root=Path(__file__).resolve().parents[1]
    for path in ('research/ict_smc_expansion.py','research/ict_smc_fixtures.py','docs/strategy_library/ICT_SMC_EXPANSION.md'):
        files[path]=hashlib.sha256((root/path).read_bytes()).hexdigest()
    return canonical_hash(files),files


def synthetic():
    artifact,files=artifact_identity();results=[]
    for d in DEFINITIONS:
        for direction in (1,-1):
            bars,context=snapshot(d,direction);last=bars[-1];price=last.close
            entry=replace(last,open_timestamp=last.close_timestamp,close_timestamp=last.close_timestamp+900,open=price,high=price+.1,low=price-.1,close=price)
            if context and entry.close_timestamp%3600==0:
                context=context+(replace(context[-1],open_timestamp=context[-1].close_timestamp,close_timestamp=entry.close_timestamp),)
            dataset=canonical_hash({'bars':[asdict(b) for b in bars+(entry,)],'context':[asdict(b) for b in context]})
            r=ComposedResearchRuntime(d,dataset_id=dataset,artifact_digest=artifact);records=[]
            for i,b in enumerate(bars+(entry,)):
                records.extend(r.advance((bars+(entry,))[:i+1],tuple(c for c in context if c.close_timestamp<=b.close_timestamp),tick_size=.001))
            summary=_summarize(r,records,bars+(entry,),900);summary.pop('records')
            results.append({'strategy_ref':f'{d.strategy_id}@{d.version}','family':d.family,'direction':direction,**summary})
    return {'schema':'ict-smc-research-expansion.v1','evidence_class':'SYNTHETIC_FIXTURE','historical_validation':'NOT_RUN','registration_performed':False,'execution_enabled':False,'artifact_digest':artifact,'source_files':files,'results':results}


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('action',choices=['catalog','synthetic','replay']);p.add_argument('--ref',choices=BY_REF);p.add_argument('--dataset');p.add_argument('--output',required=True);a=p.parse_args()
    if a.action=='synthetic':result=synthetic()
    elif a.action=='catalog':result={'definitions':[d.manifest()|{'strategy_id':d.strategy_id,'parameter_hash':canonical_hash(d.manifest())} for d in DEFINITIONS],'registered':False}
    else:
        if not a.ref or not a.dataset:p.error('replay requires --ref and --dataset')
        result=replay(None,a.dataset,definition=BY_REF[a.ref],artifact_override=artifact_identity())
    path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n')

if __name__=='__main__':main()
