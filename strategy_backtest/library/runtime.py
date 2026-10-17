"""Offline SDK adapters for canonical evaluations and theoretical entries.

This is a small research adapter around the existing StrategyRuntime/Evaluation
boundary, not a replacement for deployed orchestration. It has no execution,
publication, database, broker or network dependencies.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import math
from typing import Mapping, Sequence

from core.strategies.evaluation import (Decision, DecisionTrace, Evaluation, StageResult,
                                        StageStatus, TraceFidelity, canonical_hash, default_reason_codes)
from strategy_backtest.models import MarketEvent
from strategy_backtest.research_outcomes import ResearchOutcomeEntry
from .catalog import Definition
from .compositions import detect
from .primitives import VERSION

STEPS = {'M5':300,'M15':900,'H1':3600,'H4':14400,'D1':86400}
ENGINE = 'composed-research-engine.v1'
CONTEXT_MODES = {'ict_fvg','fvg','liquidity_run','bos_retest','structure_pullback','ote','vwap_reclaim'}


def iso(timestamp):
    return datetime.fromtimestamp(timestamp,timezone.utc).isoformat()


def validate_series(bars: Sequence[MarketEvent], timeframe: str, instrument: str, cutoff: int):
    step=STEPS[timeframe]
    last=None
    for b in bars:
        if b.timeframe!=timeframe or b.canonical_instrument!=instrument or not b.completed:
            raise ValueError('series instrument/timeframe/completion mismatch')
        values=(b.open,b.high,b.low,b.close)
        if any(not math.isfinite(v) or v<=0 for v in values) or b.high<max(b.open,b.close) or b.low>min(b.open,b.close) or b.low>b.high:
            raise ValueError('invalid OHLC')
        if b.open_timestamp%step or b.close_timestamp-b.open_timestamp!=step:
            raise ValueError('misaligned candle')
        if b.close_timestamp>cutoff:
            raise ValueError('future candle outside as-of view')
        if last is not None and b.open_timestamp<=last:
            raise ValueError('duplicate or out-of-order candle')
        available=b.provenance.get('data_available_at')
        if available is not None and (not isinstance(available,(int,float)) or available>cutoff):
            raise ValueError('candle not yet available at cutoff')
        last=b.open_timestamp


class ComposedResearchRuntime:
    """One runtime per definition/configuration/data run; never returns executable signals.

    Call evaluate with a completed, as-of snapshot at every execution close.
    Missing snapshot boundaries cancel pending next-open research observations.
    Revisions are rejected before state mutation; checkpoints bind artifact/data.
    """
    runtime_version=ENGINE

    def __init__(self, definition: Definition, *, dataset_id: str, artifact_digest: str):
        if not dataset_id or not artifact_digest:
            raise ValueError('dataset and artifact identities required')
        if definition.execution_eligible or definition.lifecycle!='RESEARCH':
            raise ValueError('this adapter supports research-only definitions')
        self.definition=definition
        self.strategy_id=definition.strategy_id
        self.dataset_id=dataset_id
        self.artifact_digest=artifact_digest
        self.parameter_hash=canonical_hash(definition.manifest())
        self.parameter_set_id=f'{self.strategy_id}@V1:experimental:{self.parameter_hash[:16]}'
        self.dependency_hash=canonical_hash({'primitives':VERSION,'composition':definition.mode,'artifact':artifact_digest})
        self._state={'seen':{},'episodes':[],'pending':{},'last_cutoff':{},'entries':[],'tick_sizes':{}}

    def _binding(self):
        return {'schema':'composed-research-state.v1','strategy':self.strategy_id,'parameters':self.parameter_hash,
                'dependencies':self.dependency_hash,'engine':ENGINE,'dataset':self.dataset_id,'artifact':self.artifact_digest}

    def snapshot_state(self):
        return {'binding':self._binding(),'state':deepcopy(self._state)}

    def restore_state(self,snapshot):
        if snapshot.get('binding')!=self._binding():raise ValueError('incompatible checkpoint')
        self._state=deepcopy(snapshot['state'])

    def _record(self,instrument,cutoff,reason,evidence=None,*,proposal=None,entry=None):
        detail={'reason':reason,**(evidence or {})}
        code=default_reason_codes().get('LIBRARY_'+reason)
        stage=StageResult('composition',StageStatus.PASS if proposal or entry else StageStatus.FAIL,
                          f'library.{self.definition.mode}@1',observed=detail,evidence_times=(iso(cutoff),))
        trace=DecisionTrace((stage,),Decision.REJECT if proposal or entry or reason.startswith('ENTRY_') or reason in {'MISSING_NEXT_CANDLE','INVALID_ENTRY_GEOMETRY'} else Decision.NO_CANDIDATE,
                            (code,),TraceFidelity.L2)
        provenance={'research_only':True,'execution_eligible':False,'family':self.definition.family,
                    'parameter_values':self.definition.manifest(),'parameter_hash':self.parameter_hash,
                    'dependency_manifest_hash':self.dependency_hash,'primitive_version':VERSION,
                    'artifact_digest':self.artifact_digest,'dataset_manifest_hash':self.dataset_id,
                    'information_cutoff':cutoff,'availability_assumption':'CLOSE_UNLESS_SUPPLIED',
                    **detail}
        candidate=None; direction=None
        if proposal:
            candidate=proposal['episode_id'];direction=proposal['direction']
            provenance['proposal']=proposal
        if entry:
            candidate=entry['episode_id'];direction=entry['direction'];provenance['research_entry']=entry
        return Evaluation(self.strategy_id,instrument,iso(cutoff),trace.decision,trace,
                          direction=direction,strategy_version='V1',parameter_set_id=self.parameter_set_id,
                          candidate_id=candidate,reason_codes=(code,),trace_fidelity=TraceFidelity.L2,
                          runtime_version=ENGINE,evaluator_version=f'{self.definition.mode}.v1',provenance=provenance)

    def evaluate(self,inputs: Mapping,*,decision_time: str,as_of: str|None=None) -> Evaluation:
        """Protocol-compatible stateless detection; streaming replay uses advance()."""
        timestamp=datetime.fromisoformat(as_of or decision_time)
        if timestamp.tzinfo is None:raise ValueError('timezone-aware as-of required')
        cutoff=int(timestamp.timestamp())
        bars=tuple(inputs['bars']);context=tuple(inputs.get('context',()))
        if not bars:raise ValueError('bars required')
        instrument=bars[-1].canonical_instrument
        tick=inputs['tick_size']
        self._validate(bars,context,instrument,cutoff,tick)
        d=self._detect(bars,context,instrument,cutoff,tick)
        proposal=self._proposal(d.proposal,bars[-1],cutoff) if d.proposal else None
        return self._record(instrument,cutoff,d.reason,d.evidence,proposal=proposal)

    def _validate(self,bars,context,instrument,cutoff,tick):
        if isinstance(tick,bool) or not isinstance(tick,(int,float)) or not math.isfinite(tick) or tick<=0:
            raise ValueError('tick_size must be positive and finite')
        validate_series(bars,self.definition.execution,instrument,cutoff)
        if bars[-1].close_timestamp!=cutoff:raise ValueError('evaluation requires current execution close')
        validate_series(context,self.definition.context,instrument,cutoff)

    def _detect(self,bars,context,instrument,cutoff,tick):
        from .compositions import Detection
        if self.definition.symbols and instrument not in self.definition.symbols:
            return Detection('INSTRUMENT_OUTSIDE_SCOPE')
        if self.definition.mode in CONTEXT_MODES:
            boundary=cutoff//STEPS[self.definition.context]*STEPS[self.definition.context]
            if len(context)<10 or context[-1].close_timestamp!=boundary:
                return Detection('CONTEXT_UNAVAILABLE')
            window=context[-80:]
            if any(a.close_timestamp!=b.open_timestamp for a,b in zip(window,window[1:])):
                return Detection('CONTEXT_DATA_GAP')
        return detect(self.definition,bars,context,tick)

    def _proposal(self,p,bar,cutoff):
        direction='LONG' if p.direction==1 else 'SHORT'
        episode=canonical_hash({'strategy':self.strategy_id,'version':'V1','parameters':self.parameter_hash,
                                'instrument':bar.canonical_instrument,'direction':direction,'anchor':p.anchor})[:32]
        return {'episode_id':episode,'direction':direction,'anchor':p.anchor,
                'confirmation_time':cutoff,'intended_entry_time':cutoff,'entry_reference':bar.close,
                'stop':p.stop,'target':p.target,'evidence':dict(p.evidence)}

    def advance(self,bars: Sequence[MarketEvent],context: Sequence[MarketEvent],*,tick_size: float):
        """Return ordered canonical records: previous entry resolution then new setup.

        Entire entry candle is available here only to reconstruct its open. Entry
        gates never consume that candle's high/low/close; outcomes are external.
        """
        if not bars:raise ValueError('bars required')
        bars,context=tuple(bars),tuple(context)
        current=bars[-1];cutoff=current.close_timestamp;instrument=current.canonical_instrument
        self._validate(bars,context,instrument,cutoff,tick_size)
        # Detect before changing state so invalid detector input is atomic too.
        if instrument in self._state['tick_sizes'] and self._state['tick_sizes'][instrument]!=tick_size:
            raise ValueError('tick size changed; start a separately identified research run')
        detection=self._detect(bars,context,instrument,cutoff,tick_size)
        identities={f'{b.timeframe}:{b.open_timestamp}':canonical_hash({'open':b.open,'high':b.high,'low':b.low,'close':b.close,
                        'tick_volume':b.provenance.get('tick_volume'),'source':b.source}) for b in bars+context}
        seen=self._state['seen'].get(instrument,{})
        if any(key in seen and seen[key]!=value for key,value in identities.items()):
            raise ValueError('conflicting candle revision')
        last=self._state['last_cutoff'].get(instrument)
        if last is not None and cutoff<last:raise ValueError('out-of-order snapshot')
        if last==cutoff:return ()
        output=[]
        pending=self._state['pending'].get(instrument)
        accepted=None
        if pending:
            entry=current.open;stop=pending['stop'];target=pending['target']
            reason=self._entry_reason(pending, current.open_timestamp, entry)
            detail={'episode_id':pending['episode_id'],'entry_cancelled':reason!='ENTRY_ACCEPTED',
                    'intended_entry_time':pending['intended_entry_time'],
                    'reference_open_time':current.open_timestamp,'observed_at':current.provenance.get('processing_time'),
                    'data_available_at':current.provenance.get('data_available_at'),
                    'entry_classification':'COMPLETED_CANDLE_RECONSTRUCTION'}
            if reason=='ENTRY_ACCEPTED':
                accepted=ResearchOutcomeEntry(pending['episode_id'],f'{self.strategy_id}@V1',instrument,
                          pending['direction'],current.open_timestamp,entry,stop,target,self.parameter_hash,
                          self.dependency_hash,self.dataset_id,provenance=detail)
                detail.update({'entry_price':entry,'stop':stop,'target':target})
            output.append(self._record(instrument,cutoff,reason,detail,
                          entry=asdict(accepted) if accepted else None))
        proposal=self._proposal(detection.proposal,current,cutoff) if detection.proposal else None
        reason=detection.reason
        if proposal and proposal['episode_id'] in self._state['episodes']:
            proposal=None;reason='DUPLICATE_EPISODE'
        output.append(self._record(instrument,cutoff,reason,detection.evidence,proposal=proposal))
        self._state['seen'].setdefault(instrument,{}).update(identities)
        self._state['last_cutoff'][instrument]=cutoff
        self._state['tick_sizes'][instrument]=tick_size
        self._state['pending'].pop(instrument,None)
        if proposal:
            self._state['episodes'].append(proposal['episode_id'])
            self._state['pending'][instrument]=proposal
        if accepted:self._state['entries'].append(asdict(accepted))
        return tuple(output)

    def _entry_reason(self, pending, open_time, entry):
        """One geometry/gap policy shared by replay and live observation."""
        sign=1 if pending['direction']=='LONG' else -1
        stop=pending['stop'];target=pending['target']
        deadline=pending['evidence'].get('entry_deadline')
        if open_time!=pending['intended_entry_time']:return 'MISSING_NEXT_CANDLE'
        if deadline is not None and open_time>=deadline:return 'ENTRY_WINDOW_EXPIRED'
        if abs(entry-pending['entry_reference'])>self.definition.max_entry_gap_atr*pending['evidence']['prior_atr']:return 'ENTRY_GAP_CANCELLED'
        if not sign*(entry-stop)>0 or not sign*(target-entry)>0:return 'INVALID_ENTRY_GEOMETRY'
        if abs(target-entry)/abs(entry-stop)<self.definition.min_r:return 'ENTRY_RR_BELOW_MINIMUM'
        return 'ENTRY_ACCEPTED'

    def observe_open(self, observation, *, setup_available_at, now):
        """Resolve a confirmed proposal immediately, without a completed entry bar.

        This is a new opt-in runtime path. Callers must supply a provider opening
        observation, not a historical bar or an arbitrary current quote. Persist
        checkpoint and event together before publication, under a new run identity.
        """
        from core.strategies.entry_timing import CandleOpenObservation
        if not isinstance(observation, CandleOpenObservation):
            raise TypeError('CandleOpenObservation required')
        instrument=observation.instrument
        pending=self._state['pending'].get(instrument)
        if pending is None:return ()
        if observation.timeframe_seconds!=STEPS[self.definition.execution]:
            raise ValueError('entry timeframe mismatch')
        if self._state['last_cutoff'].get(instrument)!=pending['intended_entry_time']:
            raise ValueError('pending proposal no longer matches setup watermark')
        timing=observation.validate(expected_open=pending['intended_entry_time'],
                                    setup_available_at=setup_available_at, now=now)
        reason=self._entry_reason(pending, observation.open_time, observation.price)
        detail={'episode_id':pending['episode_id'], 'entry_cancelled':reason!='ENTRY_ACCEPTED',
                'entry_timing':timing, 'entry_classification':timing['entry_classification'],
                'intended_entry_time':pending['intended_entry_time'],
                'observed_at':observation.observed_at, 'data_available_at':observation.available_at}
        accepted=None
        if reason=='ENTRY_ACCEPTED':
            accepted=ResearchOutcomeEntry(pending['episode_id'],f'{self.strategy_id}@V1',instrument,
                      pending['direction'],observation.open_time,observation.price,pending['stop'],pending['target'],
                      self.parameter_hash,self.dependency_hash,self.dataset_id,provenance=detail)
            detail.update({'entry_price':observation.price,'stop':pending['stop'],'target':pending['target']})
        result=self._record(instrument,observation.open_time,reason,detail,
                            entry=asdict(accepted) if accepted else None)
        self._state['pending'].pop(instrument)
        if accepted:self._state['entries'].append(asdict(accepted))
        return (result,)

    def accepted_entries(self):
        return tuple(ResearchOutcomeEntry(**deepcopy(e)) for e in self._state['entries'])
