"""Offline counterfactual comparison; no publication, database or broker imports.

Input is a full exported observer state, not the redacted audit summary. Explicit
warmup count and tick manifest avoid inferring activation or historical metadata.
"""
import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from research.price_action_metadata import tick_size
from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_continuation import (
    EngulfingContinuationEvaluator, parameter_set as eg_parameters, strategy_version as eg_version)
from strategy_backtest.price_action_sr_retest import (
    SupportResistanceRetestEvaluator, parameter_set as sr_parameters, strategy_version as sr_version)


def replay(events, strategy, *, tick, warmup_m15_bars):
    if type(warmup_m15_bars) is not int or warmup_m15_bars <= 0:
        raise ValueError('explicit positive warmup M15 count required')
    tick=tick_size(tick)
    if strategy=='engulfing_continuation':
        evaluator=EngulfingContinuationEvaluator();evaluator.initialize(eg_version(),eg_parameters())
    elif strategy=='sr_retest':
        evaluator=SupportResistanceRetestEvaluator();evaluator.initialize(sr_version(),sr_parameters())
    else:raise ValueError('unknown PA evaluator')
    ordered=sorted((MarketEvent(**e) for e in events),key=lambda e:(e.close_timestamp,0 if e.timeframe=='H1' else 1))
    if len({e.canonical_instrument for e in ordered})!=1:
        raise ValueError('exactly one instrument required per stream')
    execution=[e for e in ordered if e.timeframe=='M15']
    if len(execution)<warmup_m15_bars:raise ValueError('insufficient history for explicit warmup')
    boundary=execution[warmup_m15_bars-1].close_timestamp
    reasons=Counter();entries=[]
    for event in ordered:
        updated=replace(event,provenance={**event.provenance,'tick_size':tick,
                                         'evidence_class':'RETROSPECTIVE_COUNTERFACTUAL'})
        outputs=evaluator.consume_market_event(updated)
        if event.close_timestamp<=boundary:
            evaluator.pending.clear()
            continue
        for evaluation in outputs:
            p=evaluation.provenance;reason=p.get('rejection_reason')
            reasons[reason]+=1
            if reason=='ENTRY_OBSERVED_RESEARCH_ONLY' and not p.get('entry_cancelled'):
                entries.append({'reference_entry_time':p['information_cutoff'],'direction':p['direction'],
                                'entry':p['entry_open'],'stop':p['stop'],'target':p['target']})
    return {'tick_size':tick,'warmup_last_close':boundary,'rejections':dict(reasons),
            'accepted_reconstructed_entries':entries,'execution_eligible':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint',type=Path,required=True)
    parser.add_argument('--tick-manifest',type=Path,required=True)
    parser.add_argument('--warmup-m15-bars',type=int,required=True)
    args=parser.parse_args()
    raw=args.checkpoint.read_bytes();ticks_raw=args.tick_manifest.read_bytes()
    state=json.loads(raw);state=state.get('state',state)
    ticks=json.loads(ticks_raw)
    report={'evidence_class':'RETROSPECTIVE_COUNTERFACTUAL','execution_eligible':False,
            'checkpoint_sha256':hashlib.sha256(raw).hexdigest(),
            'tick_manifest_sha256':hashlib.sha256(ticks_raw).hexdigest(),
            'assumption':'Supplied tick manifest is assumed applicable to retained historical bars; revision history is not established.',
            'streams':{}}
    for key,record in state.get('evaluators',{}).items():
        strategy,instrument=key.split('|',1)
        if instrument not in ticks:continue
        events=record['snapshot']['events']
        old={tick_size((e.get('provenance') or {}).get('tick_size')) for e in events}
        if len(old)!=1:raise ValueError('baseline stream contains mixed/invalid ticks')
        report['streams'][key]={'recorded_inputs':replay(events,strategy,tick=old.pop(),warmup_m15_bars=args.warmup_m15_bars),
                              'corrected_inputs':replay(events,strategy,tick=ticks[instrument],warmup_m15_bars=args.warmup_m15_bars)}
    if not report['streams']:raise ValueError('no checkpoint streams matched explicit tick manifest')
    print(json.dumps(report,indent=2,sort_keys=True))


if __name__=='__main__':main()
