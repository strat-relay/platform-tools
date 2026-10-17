from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone
from types import SimpleNamespace as S
from unittest.mock import MagicMock

import pytest

from research.price_action_forward_runtime import _read_cached_symbol
from research.price_action_forward_observer import Observer,freeze,_event
from research.price_action_metadata import POLICY,bind_rows,validate_checkpoint


def reader(tick,rows=None):
    rows=rows or [{'time':900,'open':100.,'high':101.,'low':99.,'close':100.1}]
    snapshot=S(symbol_info={'tick_size':tick},quote={},
               bars={tf:S(bars=rows,latest_completed_timestamp=900) for tf in ('H1','M15')},
               source='REDIS_CANONICAL_CACHE',cache_age_seconds=0,
               continuity_status='CONTINUOUS',gap_status='NONE',recovery_status='NONE')
    return S(get_snapshot=lambda *args:snapshot)


@pytest.mark.parametrize('instrument,tick',[('XAUUSD',.001),('BTCUSD',.01),('USDJPY',.001)])
def test_provider_tick_reaches_both_timeframes_without_mutating_cache(instrument,tick):
    rows=[{'time':900,'open':100.,'high':101.,'low':99.,'close':100.1}];before=deepcopy(rows)
    data=_read_cached_symbol(reader(tick,rows),instrument,instrument+'m')
    for tf in ('H1','M15'):
        event=_event(instrument,tf,data[tf][0],'snapshot','LIVE_MARKET')
        assert event.provenance['tick_size']==tick
        assert event.provenance['tick_size_source']=='CANONICAL_SYMBOL_METADATA'
        assert event.provenance['market_input_policy']==POLICY
    assert rows==before and 'tick_size' not in rows[0]


@pytest.mark.parametrize('tick',[None,True,0,-1,float('nan'),float('inf'),'0.001'])
def test_bad_provider_metadata_cannot_fall_back(tick):
    with pytest.raises(ValueError):_read_cached_symbol(reader(tick),'XAUUSD','XAUUSDm')
    with pytest.raises(ValueError):_event('XAUUSD','M15',{'time':900,'open':1,'high':1,'low':1,'close':1,'tick_size':tick},'x','LIVE_MARKET')


def test_conflicting_row_metadata_cannot_override_provider():
    with pytest.raises(ValueError,match='conflicts'):
        bind_rows([{'tick_size':1}],.001)


def new_observer(tmp_path):
    manifest=tmp_path/'manifest.json';freeze(t0='1970-01-01T00:00:00Z',output=manifest)
    return Observer(tmp_path/'state',manifest)


def test_legacy_checkpoint_and_mixed_history_are_rejected_without_mutation(tmp_path):
    observer=new_observer(tmp_path);observer.state.pop('market_input_policy')
    before=deepcopy(observer.state)
    with pytest.raises(ValueError,match='legacy'):
        observer.ingest('XAUUSD','XAUUSDm',[],[],observed_at='x',snapshot_id='x')
    assert observer.state==before
    state={'market_input_policy':POLICY,'instrument_tick_sizes':{'XAUUSD':.001},
           'evaluators':{'sr_retest|XAUUSD':{'snapshot':{'events':[{'provenance':{'tick_size':1,'market_input_policy':POLICY}}]}}}}
    with pytest.raises(ValueError,match='conflicts'):validate_checkpoint(state)


def test_provider_change_rejects_before_mutation(tmp_path):
    observer=new_observer(tmp_path)
    observer.state['instrument_tick_sizes']['XAUUSD']=.001
    before=deepcopy(observer.state)
    row={'time':900,'open':100,'high':101,'low':99,'close':100,'tick_size':.01}
    with pytest.raises(ValueError,match='changed'):
        observer.ingest('XAUUSD','XAUUSDm',[row],[row],observed_at='x',snapshot_id='x')
    assert observer.state==before


@pytest.mark.parametrize('strategy',['sr_retest','engulfing_continuation'])
def test_both_observers_preserve_tick_provenance_through_accepted_entry(tmp_path,strategy):
    from research.replay_price_action_sr_retest import events as sr_events
    from research.replay_price_action_engulfing_continuation import context,bar
    from strategy_backtest.models import MarketEvent
    observer=new_observer(tmp_path);observer.strategy_filter=strategy
    events=sr_events()[:-1] if strategy=='sr_retest' else context()+(bar(70,'M15',105,106,99.95,100),bar(71,'M15',100,111,99.9,110))
    def rows(events,tf):
        return bind_rows([{'time':e.open_timestamp,'open':e.open,'high':e.high,'low':e.low,'close':e.close} for e in events if e.timeframe==tf],.01)
    prefix=events[:-1]
    observer.ingest('SYNTH','SYNTHm',rows(prefix,'H1'),rows(prefix,'M15'),observed_at='2026-10-17T00:00:00Z',snapshot_id='warmup')
    proposal=observer.ingest('SYNTH','SYNTHm',rows(events,'H1'),rows(events,'M15'),observed_at='2026-10-17T00:01:00Z',snapshot_id='confirmation')
    assert any(e['event']['provenance']['rejection_reason']=='RESEARCH_PROPOSAL_ONLY' for e in proposal['emitted'])
    at=events[-1].close_timestamp;price=events[-1].close
    entry=MarketEvent('SYNTH','M15',at,at+900,price,price+1,price-1,price)
    result=observer.ingest('SYNTH','SYNTHm',rows(events,'H1'),rows(events+(entry,),'M15'),observed_at='2026-10-17T00:02:00Z',snapshot_id='entry')
    accepted=[e for e in result['emitted'] if e['event']['provenance']['rejection_reason']=='ENTRY_OBSERVED_RESEARCH_ONLY']
    assert len(accepted)==1
    p=accepted[0]['event']['provenance']
    assert p['provider_tick_size']==.01 and p['market_input_policy']==POLICY
    assert p['tick_size_source']=='CANONICAL_SYMBOL_METADATA'
    validate_checkpoint(observer.state,binding=observer.input_binding)


def test_corrected_instances_are_discovered_with_explicit_parameter_provenance():
    from orchestration.adapters.price_action_observer import adapters_for_strategy,ENGULFING_STRATEGY_ID
    from strategy_backtest.price_action_engulfing_continuation import PARAMETER_HASH
    inst={'instance_id':'corrected-unique-id','strategy_id':ENGULFING_STRATEGY_ID,'enabled':True,
          'market_input_policy':POLICY,'signal_publication':True,'publication_activation_time':1792174500,
          'parameter_set_id':'engulfing-v1-frozen','parameter_set_fingerprint':PARAMETER_HASH,
          'execution_mode':'OFF','broker_writes':False,'active_instruments':['XAUUSD']}
    conn=MagicMock();adapters=adapters_for_strategy(conn,{'instances':[inst,inst]},ENGULFING_STRATEGY_ID)
    assert len(adapters)==2 # legacy evidence drain plus one corrected managed instance
    adapter=adapters[-1]
    p={'rejection_reason':'ENTRY_OBSERVED_RESEARCH_ONLY','entry_cancelled':False,
       'entry_open':100,'stop':99,'target':102,'direction':'LONG','information_cutoff':1792174500,
       'parameter_hash':PARAMETER_HASH,'market_input_policy':POLICY,'provider_tick_size':.001,
       'tick_size_source':'CANONICAL_SYMBOL_METADATA'}
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[('event1','XAUUSD',{'event':{'provenance':p}},datetime.now(timezone.utc))]
    signal=adapter.discover_new_signals(set())[0]
    assert signal.strategy_metadata['parameter_set_id']=='engulfing-v1-frozen'
    assert signal.strategy_metadata['provider_tick_size']==.001
    assert signal.strategy_instance_id=='corrected-unique-id'
    del p['market_input_policy']
    with pytest.raises(ValueError,match='incompatible'):adapter.discover_new_signals(set())


def test_counterfactual_replay_is_repeatable_and_leaves_checkpoint_unchanged():
    from research.replay_pa_tick_correction import replay
    from research.replay_price_action_engulfing_continuation import context,bar
    events=context()+(bar(70,'M15',105,106,99.95,100),bar(71,'M15',100,111,99.9,110),bar(72,'M15',110,112,109,111))
    data=[asdict(e) for e in events];before=deepcopy(data)
    report=replay(data,'engulfing_continuation',tick=.01,warmup_m15_bars=1)
    assert len(report['accepted_reconstructed_entries'])==1
    assert replay(data,'engulfing_continuation',tick=.01,warmup_m15_bars=1)==report
    assert data==before and report['execution_eligible'] is False


def test_corrected_v2_loading_preserves_actual_uuid_even_with_legacy_alias_attribute():
    from orchestration.config import _load_v2_instances_from_database
    conn=MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[
        ('new-managed-uuid','price_action_engulfing_continuation_v1','Corrected Engulfing',True,
         ['XAUUSD'],[],{'market_input_policy':POLICY,'instance_id':'old-alias'},'fingerprint',None,'OFF',0)]
    loaded=_load_v2_instances_from_database(conn)
    assert loaded[0]['instance_id']=='new-managed-uuid'
    assert loaded[0]['strategy_id']=='PRICE_ACTION_ENGULFING_CONTINUATION'
