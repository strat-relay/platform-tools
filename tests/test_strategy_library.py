"""Independent numerical/causal checks, not profitability tests."""
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
import math

import pytest

from strategy_backtest.models import MarketEvent
from strategy_backtest.library import DEFINITIONS, EXISTING, BY_KEY, ComposedResearchRuntime
from strategy_backtest.library import primitives as p
from strategy_backtest.library.compositions import detect
from strategy_backtest.library.runtime import STEPS

BASE=1704067200 # Monday 2024-01-01 UTC


def bar(i, o=100., h=101., l=99., c=100., *, step=900, tf='M15', symbol='EURUSD', base=BASE, volume=10):
    return MarketEvent(symbol,tf,base+i*step,base+(i+1)*step,o,h,l,c,
                       provenance={'tick_volume':volume})


def series(n=240, *, tf='M15', symbol='EURUSD', end=None):
    step=STEPS[tf]
    base=BASE if end is None else end-n*step
    return tuple(bar(i,step=step,tf=tf,symbol=symbol,base=base) for i in range(n))


def runtime(d):
    return ComposedResearchRuntime(d,dataset_id='synthetic-tests',artifact_digest='test-artifact')


def test_catalog_is_fifty_distinct_definitions_ten_families_and_existing_imports():
    from collections import Counter
    counts=Counter(d.family for d in DEFINITIONS)
    counts.update(e['family'] for e in EXISTING)
    assert len(DEFINITIONS)==44 and len(EXISTING)==6
    assert len({d.strategy_id for d in DEFINITIONS}|{e['strategy_id'] for e in EXISTING})==50
    assert len(counts)==10 and set(counts.values())=={5}
    for e in EXISTING:assert importlib.util.find_spec(e['module'])
    assert all(not d.execution_eligible and d.lifecycle=='RESEARCH' for d in DEFINITIONS)


def test_indicators_numerical_contracts():
    assert p.ema([1,2,3,4],3)==3
    assert p.atr(series(16),14)==2
    assert p.rsi(list(range(20)))==100
    assert p.rsi([2]*20)==50
    assert p.rsi(list(range(20,0,-1)))==0
    assert p.bands([5]*20)==(5,5,5)
    assert p.channel(series(20),20)==(99,101)
    assert p.vwap(series(5))==100
    assert p.vwap((bar(0,volume=0),)) is None


def test_swing_known_at_and_fvg_confirmation():
    bars=(bar(0,h=102),bar(1,h=103),bar(2,h=110),bar(3,h=104),bar(4,h=102))
    assert not p.swings(bars[:-1])
    highs=[x for x in p.swings(bars) if x.kind=='HIGH']
    assert highs[0].event_at==bars[2].close_timestamp
    assert highs[0].known_at==bars[4].close_timestamp
    gaps=p.fvgs((bar(0),bar(1,101,105,100,104),bar(2,104,106,103,105)))
    assert gaps[0].low==101 and gaps[0].high==103
    assert gaps[0].known_at==BASE+2700


def test_session_dst_cross_midnight_and_complete_range():
    winter=int(datetime(2024,1,15,15,tzinfo=timezone.utc).timestamp())
    summer=int(datetime(2024,7,15,15,tzinfo=timezone.utc).timestamp())
    a,_,_=p.session_bounds(winter,'America/New_York',570,600)
    b,_,_=p.session_bounds(summer,'America/New_York',570,600)
    assert datetime.fromtimestamp(a,timezone.utc).hour==14
    assert datetime.fromtimestamp(b,timezone.utc).hour==13
    a,b,date=p.session_bounds(BASE+3600,'UTC',1380,120)
    assert date=='2023-12-31' and b-a==3*3600
    assert p.session_range(series(4),BASE,BASE+3600,900)==(99,101)
    assert p.session_range(series(4)[1:],BASE,BASE+3600,900) is None
    with pytest.raises(ValueError,match='ambiguous'):
        p.session_bounds(int(datetime(2024,11,3,15,tzinfo=timezone.utc).timestamp()),'America/New_York',90,180)


def test_donchian_excludes_signal_and_entry_ignores_future_extremes():
    d=BY_KEY['donchian_breakout'];r=runtime(d)
    history=series(40)
    signal=bar(40,100,104,99.5,103)
    out=r.advance(history+(signal,),(),tick_size=.01)
    proposal=out[-1].provenance['proposal']
    assert proposal['evidence']['channel']==[99,101]
    assert proposal['direction']=='LONG'
    snap=r.snapshot_state()
    entry=bar(41,103,104,102,103)
    left=r.advance(history+(signal,entry),(),tick_size=.01)
    other=runtime(d);other.restore_state(snap)
    right=other.advance(history+(signal,replace(entry,high=1000,low=.1,close=1)),(),tick_size=.01)
    assert left[0].provenance['research_entry']==right[0].provenance['research_entry']
    assert len(r.accepted_entries())==1
    assert not left[0].provenance['execution_eligible']


@pytest.mark.parametrize('d',DEFINITIONS,ids=lambda d:d.key)
def test_every_definition_runs_canonically_and_rejects_future_input_atomically(d):
    symbol=d.symbols[0] if d.symbols else 'EURUSD'
    bars=series(240,tf=d.execution,symbol=symbol)
    cutoff=bars[-1].close_timestamp
    boundary=cutoff//STEPS[d.context]*STEPS[d.context]
    context=series(80,tf=d.context,symbol=symbol,end=boundary)
    r=runtime(d)
    result=r.advance(bars,context,tick_size=.01)
    assert len(result)==1
    assert result[0].canonical_bytes()
    assert result[0].parameter_set_id==r.parameter_set_id
    snapshot=r.snapshot_state()
    with pytest.raises(ValueError,match='future'):
        r.advance(bars,context+(replace(context[-1],open_timestamp=boundary,
                    close_timestamp=boundary+STEPS[d.context]),),tick_size=.01)
    assert r.snapshot_state()==snapshot
    assert r.advance(bars,context,tick_size=.01)==()
    restored=runtime(d);restored.restore_state(json.loads(json.dumps(snapshot)))
    snapshot['state']['episodes'].append('external-mutation')
    assert 'external-mutation' not in restored.snapshot_state()['state']['episodes']


@pytest.mark.parametrize('direction',[1,-1])
def test_breakout_full_lifecycle_gap_cancellation_and_restart(direction):
    d=BY_KEY['donchian_breakout'];r=runtime(d);history=series(40)
    signal=bar(40,100,104,99.5,103) if direction==1 else bar(40,100,100.5,96,97)
    proposal=r.advance(history+(signal,),(),tick_size=.01)[0]
    assert proposal.provenance['proposal']['direction']==('LONG' if direction==1 else 'SHORT')
    snap=r.snapshot_state();restored=runtime(d);restored.restore_state(snap)
    entry=bar(41,signal.close,signal.close+1,signal.close-1,signal.close)
    assert [o.canonical_bytes() for o in r.advance(history+(signal,entry),(),tick_size=.01)]==[o.canonical_bytes() for o in restored.advance(history+(signal,entry),(),tick_size=.01)]
    cancelled=runtime(d);cancelled.restore_state(snap)
    gap=replace(entry,open=entry.open+direction*3,high=entry.high+3,low=entry.low-3)
    assert cancelled.advance(history+(signal,gap),(),tick_size=.01)[0].provenance['reason']=='ENTRY_GAP_CANCELLED'


def test_revision_invalid_tick_and_checkpoint_binding():
    d=BY_KEY['donchian_breakout'];r=runtime(d);bars=series(40)
    for tick in (0,-1,float('nan'),True):
        before=r.snapshot_state()
        with pytest.raises(ValueError):r.advance(bars,(),tick_size=tick)
        assert r.snapshot_state()==before
    r.advance(bars,(),tick_size=.01)
    with pytest.raises(ValueError,match='revision'):
        r.advance(bars[:-1]+(replace(bars[-1],high=102),),(),tick_size=.01)
    with pytest.raises(ValueError,match='incompatible'):
        runtime(BY_KEY['ema_pullback']).restore_state(r.snapshot_state())


def test_opening_range_does_not_fire_early_or_on_missing_constituent():
    d=BY_KEY['opening_range_break']
    # 2024-01-01 New York 10:15 = 15:15Z. Range 14:30–15:00Z.
    bars=series(61)
    bars=bars[:-1]+(replace(bars[-1],open=100,high=104,low=99,close=103),)
    result=detect(d,bars,(),.01)
    assert result.reason=='PROPOSAL'
    missing=tuple(b for b in bars if b.open_timestamp!=BASE+14*3600+30*60)
    assert detect(d,missing,(),.01).reason in {'INCOMPLETE_RANGE','EXECUTION_DATA_GAP'}
    assert detect(d,bars[:-2],(),.01).reason=='OUTSIDE_SESSION'

@pytest.mark.parametrize('d',DEFINITIONS,ids=lambda d:d.key)
@pytest.mark.parametrize('short',[False,True],ids=['long','short'])
def test_all_compositions_positive_full_lifecycle_and_restart(d,short):
    from research.strategy_library_fixtures import snapshot
    bars,context=snapshot(d)
    if short:
        def mirror(b):return replace(b,open=200-b.open,high=200-b.low,low=200-b.high,close=200-b.close)
        bars=tuple(mirror(b) for b in bars);context=tuple(mirror(b) for b in context)
    r=runtime(d)
    proposal=r.advance(bars,context,tick_size=.01)[-1]
    assert proposal.provenance['reason']=='PROPOSAL', proposal.provenance
    assert proposal.direction==('SHORT' if short else 'LONG')
    pld=proposal.provenance['proposal']
    entry=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,
                  close_timestamp=bars[-1].close_timestamp+STEPS[d.execution],
                  open=bars[-1].close,high=bars[-1].close+.2,low=bars[-1].close-.2,close=bars[-1].close)
    # Advance context only when the new entry close crosses a required boundary.
    boundary=entry.close_timestamp//STEPS[d.context]*STEPS[d.context]
    if context[-1].close_timestamp<boundary:
        last=context[-1]
        context=context+(replace(last,open_timestamp=last.close_timestamp,close_timestamp=boundary),)
    checkpoint=json.loads(json.dumps(r.snapshot_state()))
    other=runtime(d);other.restore_state(checkpoint)
    outputs=r.advance(bars+(entry,),context,tick_size=.01)
    restored=other.advance(bars+(entry,),context,tick_size=.01)
    assert [o.canonical_bytes() for o in outputs]==[o.canonical_bytes() for o in restored]
    assert outputs[0].provenance['reason']=='ENTRY_ACCEPTED'
    accepted=r.accepted_entries()[0]
    assert accepted.episode_id==pld['episode_id']
    assert accepted.entry_timestamp==entry.open_timestamp
    assert accepted.entry_price==entry.open
    assert abs(accepted.target_price-accepted.entry_price)/abs(accepted.entry_price-accepted.stop_price)>=d.min_r

@pytest.mark.parametrize('d',DEFINITIONS,ids=lambda d:d.key)
def test_all_definitions_prefix_and_future_entry_independence(d):
    from research.strategy_library_fixtures import snapshot
    bars,context=snapshot(d);r=runtime(d)
    proposal=r.advance(bars,context,tick_size=.01)[-1]
    checkpoint=r.snapshot_state()
    entry=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,
                  close_timestamp=bars[-1].close_timestamp+STEPS[d.execution],
                  open=bars[-1].close,high=bars[-1].close+.2,low=bars[-1].close-.2,close=bars[-1].close)
    boundary=entry.close_timestamp//STEPS[d.context]*STEPS[d.context]
    if context[-1].close_timestamp<boundary:
        context=context+(replace(context[-1],open_timestamp=context[-1].close_timestamp,close_timestamp=boundary),)
    outputs=r.advance(bars+(entry,),context,tick_size=.01)
    changed=runtime(d);changed.restore_state(checkpoint)
    altered=replace(entry,high=190,low=1,close=50)
    alt=changed.advance(bars+(altered,),context,tick_size=.01)
    assert outputs[0].provenance['research_entry']==alt[0].provenance['research_entry']
    # Re-evaluating the original prefix has no hidden dependence on future bars.
    pure=runtime(d)
    initial_context=tuple(c for c in context if c.close_timestamp<=bars[-1].close_timestamp)
    again=pure.evaluate({'bars':bars,'context':initial_context,'tick_size':.01},
                         decision_time=__import__('strategy_backtest.library.runtime',fromlist=['iso']).iso(bars[-1].close_timestamp))
    assert proposal.canonical_bytes()==again.canonical_bytes()


def test_volume_nan_and_false_are_unavailable():
    assert p.vwap((bar(0,volume=float('nan')),)) is None
    assert p.vwap((bar(0,volume=False),)) is None


def test_definition_rejects_unsafe_or_invalid_configuration():
    with pytest.raises(ValueError):replace(BY_KEY['donchian_breakout'],execution_eligible=True)
    with pytest.raises(ValueError):replace(BY_KEY['donchian_breakout'],min_r=float('nan'))
    with pytest.raises(ValueError):replace(BY_KEY['donchian_breakout'],lookback=0)


def test_replay_cli_dataset_manifest_and_counts(tmp_path):
    from dataclasses import asdict
    from research.strategy_library import replay
    history=series(40)
    signal=bar(40,100,104,99.5,103)
    entry=bar(41,103,104,102,103)
    data={'source':{'kind':'synthetic_test'},'timestamp_semantics':'UTC_OPEN_SECONDS',
          'tick_size':.01,'bars':[asdict(b) for b in history+(signal,entry)],
          'context':[],'evidence_class':'SYNTHETIC_FIXTURE'}
    file=tmp_path/'data.json';file.write_text(json.dumps(data))
    report=replay('donchian_breakout',file)
    assert report['report']['counts_reconcile']
    assert report['report']['proposals']==1
    assert report['report']['accepted_reconstructed_entries']==1
    assert report['report']['outcomes'][0]['status']=='OPEN_AT_DATASET_END'
    assert report['dataset_sha256']
    assert report==replay('donchian_breakout',file)


def test_tick_revision_and_naive_cutoff_rejected():
    d=BY_KEY['donchian_breakout'];r=runtime(d);bars=series(40)
    r.advance(bars,(),tick_size=.01)
    with pytest.raises(ValueError,match='tick size changed'):
        r.advance(bars,(),tick_size=.1)
    with pytest.raises(ValueError,match='timezone-aware'):
        r.evaluate({'bars':bars,'tick_size':.01},decision_time='2024-01-01T10:00:00')


def test_all_definition_shared_resolver_barrier_fixtures():
    from research.strategy_library import synthetic
    report=synthetic()
    assert report['all_counts_reconcile'] and report['all_have_accepted_entry']
    assert report['scenarios']==88
    for scenario in report['reports']:
        fixtures=scenario['synthetic_barrier_fixtures']
        assert fixtures['TP']['status']=='TP'
        assert fixtures['SL']['status']=='SL'
        assert fixtures['TP']['realized_r']>0
        assert fixtures['SL']['realized_r']==pytest.approx(-1)
