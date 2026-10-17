"""Causality, replay parity, restart safety and cross-strategy execution isolation."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
import pytest

from core.strategies.entry_timing import CandleOpenObservation, OBSERVED, VERSION
from orchestration.entry_timing import publication_timing
from strategy_backtest.library import BY_KEY, ComposedResearchRuntime
from strategy_backtest.models import MarketEvent

BASE = 1792174500  # aligned M15 boundary


def observation(at=BASE, price=103, delay=2):
    return CandleOpenObservation('EURUSD', 900, at, price, at+1, at+delay, 'provider-open-1', 'MT5_CACHE')


def seeded():
    r = ComposedResearchRuntime(BY_KEY['donchian_breakout'], dataset_id='new-next-open-run', artifact_digest='test')
    bars = tuple(MarketEvent('EURUSD','M15',BASE+(i-41)*900,BASE+(i-40)*900,
                            100,101,99,100) for i in range(40))
    signal = MarketEvent('EURUSD','M15',BASE-900,BASE,100,104,99.5,103)
    result = r.advance(bars+(signal,),(),tick_size=.01)
    assert result[0].provenance['proposal']
    return r, bars+(signal,)


@pytest.mark.parametrize('price',[103,115,98])
def test_immediate_entry_matches_replay_gates_and_cannot_duplicate_after_restart(price):
    live, bars = seeded()
    replay, _ = seeded()
    result = live.observe_open(observation(price=price), setup_available_at=BASE+2, now=BASE+2)
    candle = MarketEvent('EURUSD','M15',BASE,BASE+900,price,max(price,104),min(price,102),price)
    reconstructed = replay.advance(bars+(candle,),(),tick_size=.01)[0]
    assert result[0].provenance['reason'] == reconstructed.provenance['reason']
    assert result[0].provenance['information_cutoff'] == BASE
    assert result[0].provenance['entry_timing']['entry_classification'] == OBSERVED
    restored, _ = seeded()
    restored.restore_state(live.snapshot_state())
    assert restored.observe_open(observation(price=price),setup_available_at=BASE+2,now=BASE+2) == ()
    later = restored.advance(bars+(candle,),(),tick_size=.01)
    assert not any(e.provenance.get('research_entry') for e in later)


@pytest.mark.parametrize('change',[
    {'price':float('nan')}, {'price':True}, {'open_time':BASE+900},
    {'available_at':BASE-1}, {'observed_at':BASE+100}, {'timeframe_seconds':3600},
    {'source_event_id':''}, {'available_at':BASE+3},
])
def test_bad_open_evidence_is_atomic(change):
    r,_ = seeded(); before=r.snapshot_state()
    with pytest.raises(ValueError):
        r.observe_open(replace(observation(),**change),setup_available_at=BASE+2,now=BASE+2)
    assert r.snapshot_state()==before


def test_no_entry_candle_is_required_or_exposed_to_detection():
    r,bars = seeded()
    with patch.object(r,'_detect',side_effect=AssertionError('entry observation must not detect')):
        accepted=r.observe_open(observation(),setup_available_at=BASE+2,now=BASE+2)[0]
    assert accepted.provenance['research_entry']['entry_price']==103
    assert accepted.provenance['research_entry']['entry_timestamp']==BASE
    assert r.snapshot_state()['state']['last_cutoff']['EURUSD']==BASE


def test_collector_open_evidence_preserves_first_availability_and_revision_fails_closed():
    from market_data_cache.opening import opening_evidence,read_opening
    rates={'M15':{'rates':[{'time':BASE,'open':103,'high':999,'low':1,'close':777}]}}
    first=opening_evidence(rates,{},BASE+1)
    assert first==opening_evidence(rates,first,BASE+20)
    assert 'high' not in first['M15']
    observed=read_opening({'opening_observations':first},'EURUSD','EURUSDm','M15',observed_at=BASE+2)
    assert observed.price==103 and observed.available_at==BASE+1
    rates['M15']['rates'][0]['open']=104
    revised=opening_evidence(rates,first,BASE+3)
    assert read_opening({'opening_observations':revised},'EURUSD','EURUSDm','M15',observed_at=BASE+4) is None
    assert opening_evidence(rates,revised,BASE+5)==revised


def test_publication_cannot_promote_completed_bar_pa_even_if_adapter_claims_prospective():
    from test_canonical_signal_authority import accepted_signal
    original=replace(accepted_signal(),provenance={'source_process':'research.price_action_forward_runtime',
                                                 'classification':'PROSPECTIVE_ORCHESTRATOR_SIGNAL'})
    result=publication_timing(original)
    assert result.strategy_metadata['signal_emission_class']=='RETROSPECTIVE_RESEARCH_EVIDENCE'
    assert result.strategy_metadata['execution_eligible'] is False
    assert result.signal_timestamp==original.signal_timestamp
    assert original.provenance['classification']=='PROSPECTIVE_ORCHESTRATOR_SIGNAL'


def test_publication_checks_availability_and_expired_window_without_retimestamping():
    from test_canonical_signal_authority import accepted_signal
    stamp=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    timing=observation().validate(expected_open=BASE,setup_available_at=BASE+2,now=BASE+2)
    signal=replace(accepted_signal(),signal_timestamp=stamp(BASE),signal_emitted_at=stamp(BASE+3),
                   strategy_metadata={'entry_timing':timing})
    assert publication_timing(signal).strategy_metadata.get('timing_rejection_reason') is None
    late=publication_timing(replace(signal,signal_emitted_at=stamp(BASE+61)))
    assert late.strategy_metadata['timing_rejection_reason']=='ENTRY_PUBLICATION_WINDOW_EXPIRED'
    assert late.signal_timestamp==signal.signal_timestamp
    with pytest.raises(ValueError):
        publication_timing(replace(signal,strategy_metadata={'entry_timing':{**timing,'entry_data_available_at':BASE+4}}))


@pytest.mark.parametrize('metadata,provenance',[
    ({'research_only':True},{}), ({'execution_eligible':False},{}),
    ({'signal_emission_class':'RETROSPECTIVE_RESEARCH_EVIDENCE'},{}),
    ({},{'classification':'RETROSPECTIVE_RESEARCH_EVIDENCE'}),
])
def test_future_strategy_research_metadata_blocks_before_intent_insert(metadata,provenance):
    from test_execution_v2_intent import CreateExecutionIntentTests, policy, NOW, ACCOUNT
    from execution_v2.intent import create_execution_intent
    conn=CreateExecutionIntentTests()._seeded_conn(strategy_id='FUTURE_STRATEGY',strategy_metadata=metadata,source_provenance=provenance)
    result=create_execution_intent(conn,signal_id='SIG1',account_id=ACCOUNT,risk_policy=policy(),now_utc=NOW)
    assert result.status=='BLOCKED' and result.execution_intent_id is None
    assert not conn.view("execution_v2.execution_intent")


@pytest.mark.parametrize('kind',['sr','engulfing'])
def test_pa_live_adapter_reuses_frozen_entry_checks_and_checkpoint_deduplicates(kind):
    from strategy_backtest.next_open import observe_price_action_open
    if kind=='sr':
        from strategy_backtest.price_action_sr_retest import SupportResistanceRetestEvaluator as Evaluator,parameter_set,strategy_version
        from research.replay_price_action_sr_retest import events
        history=events()[:-1];price=116
    else:
        from strategy_backtest.price_action_engulfing_continuation import EngulfingContinuationEvaluator as Evaluator,parameter_set,strategy_version
        from research.replay_price_action_engulfing_continuation import context,bar
        history=context()+(bar(70,'M15',105,106,99.95,100),bar(71,'M15',100,111,99.9,110));price=110
    r=Evaluator();r.initialize(strategy_version(),parameter_set())
    for event in history:r.consume_market_event(event)
    assert r.pending
    expected=history[-1].close_timestamp
    replay=deepcopy(r)
    opening=CandleOpenObservation('SYNTH',900,expected,price,expected+1,expected+2,'open-1','TEST')
    before_history=len(r.events)
    result=observe_price_action_open(r,opening,setup_available_at=expected+2,now=expected+2)[0]
    historical=replay.consume_market_event(MarketEvent('SYNTH','M15',expected,expected+900,price,price,price,price))[0]
    assert result.provenance['rejection_reason']==historical.provenance['rejection_reason']
    assert result.provenance['entry_open']==historical.provenance['entry_open']
    assert len(r.events)==before_history
    assert result.provenance['information_cutoff']==expected
    restored=Evaluator();restored.initialize(strategy_version(),parameter_set());restored.restore_state(r.snapshot_state())
    assert observe_price_action_open(restored,opening,setup_available_at=expected+2,now=expected+2)==()


def test_forward_library_entry_is_emitted_before_entry_candle_exists_and_survives_restart():
    from test_strategy_library_forward import LibraryForwardTests,cached
    from research.strategy_library_forward import ForwardObserver
    helper=LibraryForwardTests()
    observer,d,bars,context,symbol=helper.observer()
    config=replace(observer.config,run_id='separate-next-open-run',entry_mode=OBSERVED)
    observer=ForwardObserver(config,artifact='new-mode-artifact')
    helper.bootstrap(observer,d,bars,context,symbol)
    at=bars[-1].close_timestamp
    snapshot=cached(d,bars,context,at+2)
    snapshot.opening_observations={d.execution:{'open_time':at,'price':bars[-1].close,'available_at':at+1}}
    records=observer.process(d.key,symbol,snapshot,observed_at=at+2)
    entries=[r for r in records if r.get('payload',{}).get('provenance',{}).get('research_entry')]
    assert len(entries)==1 and entries[0]['evidence_class']==OBSERVED
    assert entries[0]['observed_at']-entries[0]['decision_time']==2
    other=ForwardObserver(config,artifact='new-mode-artifact');other.restore(observer.state)
    repeated=other.process(d.key,symbol,snapshot,observed_at=at+3)
    assert not any(r.get('payload',{}).get('provenance',{}).get('research_entry') for r in repeated)


def test_unknown_strategy_cannot_claim_fifteen_minute_old_entry_is_prospective():
    from test_canonical_signal_authority import accepted_signal
    signal=replace(accepted_signal(),strategy_id='FUTURE_UNKNOWN',
                   signal_emitted_at='2026-09-22T10:14:00Z',
                   strategy_metadata={'signal_emission_class':'PROSPECTIVE'})
    result=publication_timing(signal)
    assert result.strategy_metadata['signal_emission_class']=='RETROSPECTIVE_RESEARCH_EVIDENCE'
    assert result.strategy_metadata['timing_rejection_reason']=='ENTRY_PUBLICATION_WINDOW_EXPIRED'


from strategy_backtest.library import DEFINITIONS
@pytest.mark.parametrize('definition',DEFINITIONS,ids=lambda d:d.key)
def test_every_library_composition_can_resolve_next_open_without_entry_candle(definition):
    from research.strategy_library_fixtures import snapshot
    from strategy_backtest.library.runtime import STEPS
    bars,context=snapshot(definition)
    runtime=ComposedResearchRuntime(definition,dataset_id='new-live-run',artifact_digest='live-test')
    proposal=runtime.advance(bars,context,tick_size=.01)[-1]
    assert proposal.provenance['proposal']
    at=bars[-1].close_timestamp;instrument=bars[-1].canonical_instrument
    opened=CandleOpenObservation(instrument,STEPS[definition.execution],at,bars[-1].close,
                                 at+1,at+2,'open-1','TEST_PROVIDER')
    result=runtime.observe_open(opened,setup_available_at=at+2,now=at+2)[0]
    assert result.provenance['reason']=='ENTRY_ACCEPTED'
    assert result.provenance['research_entry']['episode_id']==proposal.provenance['proposal']['episode_id']
    assert result.provenance['information_cutoff']==at


def test_reversal_family_uses_same_immediate_entry_adapter():
    from test_price_action_reversal_v1 import CONTRACTS,lifecycle_events
    from strategy_backtest.next_open import observe_price_action_open
    for cls,version,parameters in CONTRACTS:
        r=cls();r.initialize(version(),parameters())
        events=lifecycle_events(cls)
        for event in events[:-1]:r.consume_market_event(event)
        assert r.pending
        candle=events[-1]
        opened=CandleOpenObservation(candle.canonical_instrument,900,candle.open_timestamp,candle.open,
                                     candle.open_timestamp+1,candle.open_timestamp+2,'open-1','TEST')
        result=observe_price_action_open(r,opened,setup_available_at=candle.open_timestamp+2,now=candle.open_timestamp+2)[0]
        assert result.provenance['rejection_reason']=='ENTRY_OBSERVED_RESEARCH_ONLY'
        assert result.provenance['completed_candle_reconstruction'] is False
        assert result.candidate_id==result.provenance['candidate_id']


def test_immediate_library_signal_survives_canonical_conversion_with_timing_contract():
    from test_strategy_library_orchestration import fixture,T
    from orchestration.adapters.strategy_library import StrategyLibraryAdapter
    from unittest.mock import MagicMock
    inst,record=fixture()
    timing=CandleOpenObservation('XAUUSD',900,T,100,T+1,T+2,'open-live-1','MT5_CACHE').validate(
        expected_open=T,setup_available_at=T+2,now=T+2)
    record.update(observed_at=T+2,decision_time=T,evidence_class=OBSERVED)
    record['payload']['provenance'].update(information_cutoff=T,entry_timing=timing)
    signal=StrategyLibraryAdapter(MagicMock(),'donchian_breakout',inst).convert('live-open-1','XAUUSD',record)
    signal=publication_timing(signal)
    assert signal.strategy_metadata['signal_emission_class']=='PROSPECTIVE'
    assert signal.strategy_metadata['research_only'] is True
    assert signal.strategy_metadata['execution_eligible'] is False
    assert signal.strategy_metadata['entry_timing']==timing


def test_pa_frozen_instances_cannot_be_silently_switched_to_new_mode(tmp_path,monkeypatch):
    from research.price_action_forward_runtime import run_once
    import json
    manifest=tmp_path/'new-manifest.json';manifest.write_text(json.dumps({'entry_mode':OBSERVED}))
    monkeypatch.setenv('PRICE_ACTION_INSTANCE_ID','7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104')
    monkeypatch.setenv('PRICE_ACTION_STRATEGY','sr_retest')
    monkeypatch.setenv('PRICE_ACTION_MANIFEST_PATH',str(manifest))
    monkeypatch.setenv('PRICE_ACTION_ENTRY_MODE',OBSERVED)
    with patch('research.price_action_forward_runtime.default_store',side_effect=AssertionError('must reject before cache/DB access')):
        with pytest.raises(ValueError,match='new instance'):
            run_once()


def test_new_pa_observer_persists_entry_before_candle_close(tmp_path):
    from research.price_action_next_open import NextOpenObserver
    from research.price_action_forward_observer import MANIFEST
    from research.replay_price_action_sr_retest import events
    from strategy_backtest.price_action_sr_retest import SupportResistanceRetestEvaluator,parameter_set,strategy_version
    observer=NextOpenObserver(tmp_path,MANIFEST,strategy_filter='sr_retest')
    evaluator=SupportResistanceRetestEvaluator();evaluator.initialize(strategy_version(),parameter_set())
    history=events()[:-1]
    for event in history:evaluator.consume_market_event(event)
    key='sr_retest|SYNTH'
    observer.state['evaluators'][key]={'snapshot':evaluator.snapshot_state(),'evaluations':0,'rejections':{}}
    at=history[-1].close_timestamp
    opening=CandleOpenObservation('SYNTH',900,at,116,at+1,at+2,'open-1','TEST')
    stamp=datetime.fromtimestamp(at+2,timezone.utc).isoformat()
    result=observer.ingest_open('SYNTH','SYNTHm',opening,observed_at=stamp,snapshot_id='opening-snapshot')
    assert len(result)==1
    assert result[0]['event']['provenance']['rejection_reason']=='ENTRY_OBSERVED_RESEARCH_ONLY'
    assert observer.state['entries']
    assert not observer.state['evaluators'][key]['snapshot']['pending']
    assert observer.ingest_open('SYNTH','SYNTHm',opening,observed_at=stamp,snapshot_id='opening-snapshot')==[]
