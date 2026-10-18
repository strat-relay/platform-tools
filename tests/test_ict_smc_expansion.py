from dataclasses import replace
import pytest
from strategy_backtest.library.expansion import DEFINITIONS
from strategy_backtest.library import BY_KEY,ComposedResearchRuntime
from strategy_backtest.library.compositions import detect
from strategy_backtest.library import sequence_primitives as q
from research.ict_smc_fixtures import snapshot

@pytest.mark.parametrize('d',DEFINITIONS,ids=lambda d:d.key)
@pytest.mark.parametrize('direction',[1,-1])
def test_causal_full_lifecycle_restart_and_version_provenance(d,direction):
    bars,context=snapshot(d,direction)
    detection=detect(d,bars,context,.001)
    assert detection.reason=='PROPOSAL',detection
    assert detection.proposal.direction==direction
    r=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test')
    out=r.advance(bars,context,tick_size=.001)
    assert out[-1].provenance['reason']=='PROPOSAL'
    assert out[-1].strategy_version==d.version
    saved=r.snapshot_state();restored=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test');restored.restore_state(saved)
    opening=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,close_timestamp=bars[-1].close_timestamp+900,open=bars[-1].close,high=bars[-1].close+.1,low=bars[-1].close-.1,close=bars[-1].close)
    # H1 boundary must be causally covered for OB runtime.
    if context and opening.close_timestamp%3600==0:
        context=context+(replace(context[-1],open_timestamp=context[-1].close_timestamp,close_timestamp=opening.close_timestamp),)
    a=r.advance(bars+(opening,),context,tick_size=.001);b=restored.advance(bars+(opening,),context,tick_size=.001)
    assert [x.to_dict() for x in a]==[x.to_dict() for x in b]
    assert len(r.accepted_entries())==1
    assert r.accepted_entries()[0].strategy_version.endswith('@'+d.version)
    assert not r.advance(bars+(opening,),context,tick_size=.001)
    assert r.snapshot_state()['state']['pending']=={}


def test_frozen_v1_catalog_definitions_unchanged():
    assert BY_KEY['judas_swing'].version=='V1' and BY_KEY['judas_swing'].mode=='range_fade'
    assert BY_KEY['order_block_retracement'].version=='V1' and BY_KEY['order_block_retracement'].mode=='order_block'

@pytest.mark.parametrize('d',DEFINITIONS[:2])
def test_session_sequence_requires_later_reclaim_and_displacement_and_complete_range(d):
    bars,context=snapshot(d)
    assert detect(d,bars[:-1],context,.001).proposal is None
    assert detect(d,bars[:-2],context,.001).proposal is None
    missing=tuple(b for i,b in enumerate(bars) if i!=35)
    assert detect(d,missing,context,.001).reason in {'INCOMPLETE_RANGE','EXECUTION_DATA_GAP'}
    ambiguous=bars[:-3]+(replace(bars[-3],high=111.),)+bars[-2:]
    assert detect(d,ambiguous,context,.001).reason=='AMBIGUOUS_SESSION_SWEEP'


def test_order_block_is_known_at_displacement_and_consumed_on_first_touch():
    d=DEFINITIONS[2];bars,context=snapshot(d)
    zone=q.displacement_block(bars,len(bars)-2,1,lookback=20,atr_period=14,multiple=1.2)
    assert zone.known_at==bars[-2].close_timestamp and zone.event_at<zone.known_at
    assert q.first_block_touch(zone,bars[:-1])==('BLOCK_UNTOUCHED',None)
    invalid=replace(bars[-1],open=99.,high=99.4,low=98.,close=98.5)
    assert q.first_block_touch(zone,(invalid,))[0]=='BLOCK_INVALIDATED'
    failed=replace(bars[-1],open=100.,high=100.3,low=99.6,close=100.1)
    assert detect(d,bars[:-1]+(failed,),context,.001).reason=='BLOCK_RETEST_NOT_CONFIRMED'
    later=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,close_timestamp=bars[-1].close_timestamp+900)
    assert detect(d,bars[:-1]+(failed,later),context,.001).proposal is None


def test_revisions_future_inputs_and_cross_version_restore_fail_without_state_mutation():
    d=DEFINITIONS[0];bars,context=snapshot(d);r=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test');r.advance(bars,context,tick_size=.001);before=r.snapshot_state()
    with pytest.raises(ValueError):r.advance(bars[:-1]+(replace(bars[-1],high=105.),),context,tick_size=.001)
    assert r.snapshot_state()==before
    old=ComposedResearchRuntime(BY_KEY[d.key],dataset_id='synthetic',artifact_digest='test')
    with pytest.raises(ValueError):old.restore_state(before)

@pytest.mark.parametrize('d',DEFINITIONS)
def test_next_open_gap_cancellation_ignores_future_candle_extremes(d):
    bars,context=snapshot(d);r=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test');r.advance(bars,context,tick_size=.001)
    price=bars[-1].close+10
    gap=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,close_timestamp=bars[-1].close_timestamp+900,open=price,high=price+100,low=1.,close=price)
    out=r.advance(bars+(gap,),context,tick_size=.001)
    assert out[0].provenance['reason']=='ENTRY_GAP_CANCELLED' and not r.accepted_entries()


def test_power_of_three_requires_distribution_and_rejects_wide_accumulation():
    d=DEFINITIONS[1];bars,context=snapshot(d)
    no_distribution=bars[:-1]+(replace(bars[-1],open=99.5,high=100.5,low=99.4,close=100.4),)
    assert detect(d,no_distribution,context,.001).proposal is None
    start=int(__import__('datetime').datetime(2024,1,11,tzinfo=__import__('zoneinfo').ZoneInfo(d.session_tz)).timestamp())
    wide=tuple(replace(b,high=120.) if b.open_timestamp==start+3600 else b for b in bars)
    assert detect(d,wide,context,.001).reason=='ACCUMULATION_TOO_WIDE'


def test_session_range_dst_and_entry_deadline_are_explicit():
    from strategy_backtest.library.primitives import session_bounds
    from datetime import datetime,timezone
    winter=int(datetime(2024,1,11,12,tzinfo=timezone.utc).timestamp());summer=int(datetime(2024,7,11,12,tzinfo=timezone.utc).timestamp())
    a,_,_=session_bounds(winter,'Europe/London',420,600);b,_,_=session_bounds(summer,'Europe/London',420,600)
    assert datetime.fromtimestamp(a,timezone.utc).hour==7 and datetime.fromtimestamp(b,timezone.utc).hour==6
    for d in DEFINITIONS[:2]:
        bars,context=snapshot(d);proposal=detect(d,bars,context,.001).proposal
        assert proposal.evidence['range_known_at']<proposal.evidence['sweep_at']<proposal.evidence['reclaim_at']<proposal.evidence['confirmation_at']<proposal.evidence['entry_deadline']
        r=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test')
        pending={'direction':'LONG','stop':90,'target':110,'intended_entry_time':proposal.evidence['entry_deadline'],'entry_reference':100,'evidence':proposal.evidence}
        assert r._entry_reason(pending,pending['intended_entry_time'],100)=='ENTRY_WINDOW_EXPIRED'

@pytest.mark.parametrize('d',DEFINITIONS)
def test_reconstructed_entry_uses_only_next_open_not_future_high_low_close(d):
    bars,context=snapshot(d);r=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test');r.advance(bars,context,tick_size=.001)
    other=ComposedResearchRuntime(d,dataset_id='synthetic',artifact_digest='test');other.restore_state(r.snapshot_state())
    price=bars[-1].close;entry=replace(bars[-1],open_timestamp=bars[-1].close_timestamp,close_timestamp=bars[-1].close_timestamp+900,open=price,high=price+.1,low=price-.1,close=price)
    a=r.advance(bars+(entry,),context,tick_size=.001)
    b=other.advance(bars+(replace(entry,high=price+100,low=1.,close=price+50),),context,tick_size=.001)
    assert a[0].provenance['research_entry']==b[0].provenance['research_entry']
    assert a[0].provenance['entry_classification']=='COMPLETED_CANDLE_RECONSTRUCTION'
