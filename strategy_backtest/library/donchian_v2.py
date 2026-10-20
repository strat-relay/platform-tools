"""Opt-in Donchian V2 research recipe; never registered by importing this file."""
from copy import deepcopy
from dataclasses import dataclass, replace
import math

from core.strategies.evaluation import canonical_hash
from .catalog import Definition
from .compositions import Detection, Proposal
from . import primitives as p
from .breakout_retest import VERSION, transition
from .runtime import ComposedResearchRuntime

ENGINE = 'donchian-retest-research.v2'


@dataclass(frozen=True)
class DonchianV2Definition(Definition):
    min_breakout_atr: float = .20
    breakout_body_fraction: float = .60
    breakout_close_location: float = .75
    retest_body_fraction: float = .25
    retest_close_location: float = .65
    min_layer_progress_atr: float = .50

    def __post_init__(self):
        super().__post_init__()
        if (self.version != 'V2' or self.mode != 'donchian_retest_v2' or
                self.execution != 'M15' or self.context != 'H1'):
            raise ValueError('V2 recipe requires explicit identity and H1/M15')
        for name in ('min_breakout_atr','breakout_body_fraction','breakout_close_location',
                     'retest_body_fraction','retest_close_location','min_layer_progress_atr'):
            v=getattr(self,name)
            if isinstance(v,bool) or not math.isfinite(v) or v <= 0:
                raise ValueError('positive finite V2 thresholds required')
            if ('fraction' in name or 'location' in name) and v > 1:
                raise ValueError('fraction must not exceed one')


DEFINITION = DonchianV2Definition(
    'donchian_breakout','Breakout','Donchian Breakout','donchian_retest_v2',
    'Confirmed H1 structure; directional prior-20-bar M15 breakout; first later retest holds; '
    'next-open research entry; additions require fresh pullback and favorable progress.',version='V2')


class DonchianV2Runtime(ComposedResearchRuntime):
    """Reuse shared causal entry/geometry/checkpoint boundaries, not another fill engine."""
    runtime_version = ENGINE

    def __init__(self, *, dataset_id, artifact_digest, definition=DEFINITION):
        if not isinstance(definition,DonchianV2Definition):
            raise TypeError('explicit V2 definition required')
        super().__init__(definition,dataset_id=dataset_id,artifact_digest=artifact_digest)
        self.dependency_hash=canonical_hash({'primitives':p.VERSION,'episode':VERSION,
                                           'recipe':ENGINE,'artifact':artifact_digest})
        self._state['breakout_episodes']={}
        self._staged=None

    def _binding(self):
        return super()._binding() | {'recipe':ENGINE,'episode_primitive':VERSION}

    def _record(self,*args,**kwargs):
        result=super()._record(*args,**kwargs)
        return replace(result,runtime_version=ENGINE,evaluator_version=ENGINE,
                       provenance=dict(result.provenance,recipe_version=ENGINE,episode_primitive_version=VERSION))

    def _detect(self,bars,context,instrument,cutoff,tick):
        self._staged={}
        if self.definition.symbols and instrument not in self.definition.symbols:
            return Detection('INSTRUMENT_OUTSIDE_SCOPE')
        required=max(40,self.definition.lookback+1,self.definition.atr_period+2)
        if len(bars)<required:
            return Detection('INSUFFICIENT_WARMUP')
        recent_bars=bars[-required:]
        if any(a.close_timestamp!=b.open_timestamp for a,b in zip(recent_bars,recent_bars[1:])):
            return Detection('EXECUTION_DATA_GAP')
        boundary=cutoff//3600*3600
        if len(context)<10 or context[-1].close_timestamp!=boundary:
            return Detection('CONTEXT_UNAVAILABLE')
        recent_context=context[-80:]
        if any(a.close_timestamp!=b.open_timestamp for a,b in zip(recent_context,recent_context[1:])):
            return Detection('CONTEXT_DATA_GAP')
        bias=p.trend(p.swings(context))
        atr=p.atr(bars[:-1],self.definition.atr_period)
        if atr<=0:return Detection('ZERO_VOLATILITY')
        state=self._state['breakout_episodes'].get(instrument,{})
        next_state,reason,setup=transition(state,bars,bias=bias,atr=atr,tick=tick,policy=self.definition)
        self._staged=next_state
        evidence={'prior_atr':atr,'tick_size':tick,'context_bias':bias,
                  'episode_primitive_version':VERSION,'phase':next_state.get('phase','IDLE'),
                  'setup':setup,'episode_break_at':next_state.get('break_at')}
        if setup:
            sign=bias
            stop=setup['stop_extreme']-sign*max(tick,self.definition.buffer_atr*atr)
            entry=bars[-1].close
            target=entry+sign*self.definition.target_r*abs(entry-stop)
            if not sign*(entry-stop)>0:
                self._staged['phase']='WAIT_RESET'
                return Detection('INVALID_GEOMETRY',evidence=evidence)
            evidence.update(setup,planned_r=self.definition.target_r)
            # One immutable identity per initial break and subsequent confirmation.
            anchor=f"{setup['break_at']}:{setup['kind']}:{cutoff}"
            return Detection('PROPOSAL',Proposal(sign,anchor,stop,target,evidence))
        return Detection(reason,evidence=evidence)

    def _entry_reason(self,pending,open_time,entry):
        reason=super()._entry_reason(pending,open_time,entry)
        if reason!='ENTRY_ACCEPTED':return reason
        ev=pending['evidence']
        if ev.get('kind')=='LAYER':
            sign=1 if pending['direction']=='LONG' else -1
            if sign*(entry-ev['previous_entry_price'])<self.definition.min_layer_progress_atr*ev['episode_atr']:
                return 'ENTRY_LAYER_PROGRESS_REJECTED'
        return reason

    def _after_entry(self,records,instrument,current=None):
        episode=self._state['breakout_episodes'].get(instrument,{})
        for record in records:
            entry=record.provenance.get('research_entry')
            if entry and episode.get('phase')=='ENTRY_PENDING':
                episode.update(phase='ACTIVE',last_entry_price=entry['entry_price'],
                               last_entry_time=entry['entry_timestamp'],
                               favorable_extreme=(current.high if episode['direction']==1 else current.low)
                                    if current else entry['entry_price'])
            elif record.provenance.get('entry_cancelled') and episode.get('phase')=='ENTRY_PENDING':
                episode['phase']='WAIT_RESET'

    def advance(self,bars,context,*,tick_size):
        before=deepcopy(self._state)
        self._staged=None
        try:
            records=super().advance(bars,context,tick_size=tick_size)
            if records:
                instrument=bars[-1].canonical_instrument
                self._state['breakout_episodes'][instrument]=deepcopy(self._staged or {})
                self._after_entry(records,instrument,bars[-1])
            return records
        except Exception:
            self._state=before
            raise
        finally:
            self._staged=None

    def observe_open(self,observation,*,setup_available_at,now):
        before=deepcopy(self._state)
        try:
            records=super().observe_open(observation,setup_available_at=setup_available_at,now=now)
            self._after_entry(records,observation.instrument)
            return records
        except Exception:
            self._state=before
            raise
