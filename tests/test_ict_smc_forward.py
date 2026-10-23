from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from core.strategies.evaluation import canonical_hash
from research.strategy_library_registration import registration,CANARY
from research.strategy_library_forward import RunConfig,ForwardObserver,readiness
from research.ict_smc_forward_plan import registration_sql,plan,SYMBOLS
from research.ict_smc_forward_workload import documents
from strategy_backtest.library.selection import EXPANSION_KEYS,resolve_definition
from strategy_backtest.library.catalog import BY_KEY
from strategy_backtest.library.runtime import ComposedResearchRuntime
from research.ict_smc_fixtures import snapshot
from tests.test_strategy_library_forward import cached
from orchestration.adapters.strategy_library import StrategyLibraryAdapter

IMAGE='ghcr.io/strat-relay/trading-platform-runtime@sha256:'+'a'*64

@pytest.mark.parametrize('key',EXPANSION_KEYS)
def test_versioned_registration_and_complete_observer_to_orchestrator_path(key):
    d=resolve_definition(key);identity=registration(key)
    assert identity['parameter_hash']==canonical_hash(d.manifest())
    assert identity['parameter_set_id'].startswith(d.strategy_id+'@'+d.version)
    if d.key in BY_KEY:
        old=registration(d.key)
        assert old['definition_id']==identity['definition_id']
        assert old['version_id']!=identity['version_id'] and old['instance_id']!=identity['instance_id']
    bars,ctx=snapshot(d);symbol=bars[-1].canonical_instrument
    if not ctx:
        boundary=(bars[-1].close_timestamp+900)//3600*3600
        ctx=tuple(replace(bars[-1],timeframe='H1',open_timestamp=boundary-(80-i)*3600,close_timestamp=boundary-(79-i)*3600,open=100.,high=101.,low=99.,close=100.) for i in range(80))
    activation=bars[-3].open_timestamp
    config=RunConfig('ict-forward-test',activation,(key,),{symbol:symbol+'m'})
    observer=ForwardObserver(config,artifact='frozen-test-artifact')
    before=bars[:-1];at=before[-1].close_timestamp+1
    # Bootstrap cannot emit historical entries or retain old pending proposals.
    assert not observer.process(key,symbol,cached(d,before,tuple(c for c in ctx if c.close_timestamp<=at),at),observed_at=at)
    at=bars[-1].close_timestamp+1
    observer.process(key,symbol,cached(d,bars,ctx,at),observed_at=at)
    last=bars[-1];opening=replace(last,open_timestamp=last.close_timestamp,close_timestamp=last.close_timestamp+900,open=last.close,high=last.close+.1,low=last.close-.1,close=last.close)
    at=opening.close_timestamp+1
    if ctx and ctx[-1].close_timestamp<opening.close_timestamp and opening.close_timestamp%3600==0:ctx=ctx+(replace(ctx[-1],open_timestamp=ctx[-1].close_timestamp,close_timestamp=opening.close_timestamp),)
    records=observer.process(key,symbol,cached(d,bars+(opening,),ctx,at),observed_at=at)
    accepted=next(r for r in records if r['kind']=='EVALUATION' and r['payload']['provenance'].get('research_entry'))
    inst={'instance_id':identity['instance_id'],'strategy_id':d.strategy_id,'parameter_set_fingerprint':identity['parameter_hash'],'orchestrator_run_id':config.run_id,'publication_activation_time':activation,'active_instruments':[symbol],'execution_mode':'OFF','status':'RESEARCH','signal_publication':True}
    adapter=StrategyLibraryAdapter(MagicMock(),key,inst);signal=adapter.convert(accepted['event_id'],symbol,accepted)
    assert signal.strategy_version==d.version and signal.strategy_metadata['strategy_family']==d.family
    assert signal.strategy_metadata['execution_eligible'] is False
    assert signal.strategy_metadata['signal_emission_class']=='RETROSPECTIVE_RESEARCH_EVIDENCE'
    assert signal.source_event_id.startswith('strategy-library:'+config.run_id+':')
    bad=deepcopy(accepted);bad['payload']['provenance']['research_entry']['strategy_version']=d.strategy_id+'@V99'
    with pytest.raises(ValueError):adapter.convert(bad['event_id'],symbol,bad)


def test_registration_migration_is_frozen_additive_and_offline():
    sql=registration_sql();root=Path(__file__).resolve().parents[1]
    assert sql==(root/'postgres/migrations/066_ict_smc_forward_registration.sql').read_text()
    assert 'UPDATE ' not in sql and 'CREATE ROLE' not in sql and 'GRANT ' not in sql
    assert sql.count('INSERT INTO strategy_mgmt.strategy_instance_v2')==3
    for key in EXPANSION_KEYS:
        r=registration(key)
        assert r['instance_id'] in sql and r['parameter_hash'] in sql
    for old in CANARY:assert registration(old)['instance_id'] not in sql


def test_plan_requires_digest_and_binds_immutable_run_and_eligibility():
    with pytest.raises(ValueError):plan('test',1792342800,'runtime:latest')
    manifest,sql=plan('ict-forward-test',1792342800,IMAGE)
    assert manifest['entry_mode']=='CONTEMPORANEOUS_CANDLE_OPEN'
    assert manifest['execution_eligible'] is False and manifest['symbols']==SYMBOLS
    assert 'execution_eligible=true' not in sql and "execution_mode='OFF'" in sql
    assert 'orchestrator_run_id' in sql and 'activation identity/run conflict' in sql
    docs=documents(manifest,IMAGE);deployment=docs['items'][1];container=deployment['spec']['template']['spec']['containers'][0]
    assert container['image']==IMAGE and deployment['spec']['strategy']['type']=='Recreate'
    assert container['command'][-1]=='10'
    assert '22347' not in str(docs) and '22348' not in str(docs)
    assert docs['items'][0]['immutable'] is True

@pytest.mark.parametrize('key',EXPANSION_KEYS)
def test_contemporaneous_forward_entry_publishes_correct_version_without_future_candle(key):
    d=resolve_definition(key);bars,ctx=snapshot(d);symbol=bars[-1].canonical_instrument;at=bars[-1].close_timestamp
    if not ctx:
        boundary=at//3600*3600
        ctx=tuple(replace(bars[-1],timeframe='H1',open_timestamp=boundary-(80-i)*3600,close_timestamp=boundary-(79-i)*3600,open=100.,high=101.,low=99.,close=100.) for i in range(80))
    config=RunConfig('live-open-version-test',bars[-3].open_timestamp,(key,),{symbol:symbol+'m'},entry_mode='CONTEMPORANEOUS_CANDLE_OPEN')
    observer=ForwardObserver(config,artifact='frozen-test')
    observer.process(key,symbol,cached(d,bars[:-1],ctx,bars[-2].close_timestamp+1),observed_at=bars[-2].close_timestamp+1)
    current=cached(d,bars,ctx,at+2);current.opening_observations={d.execution:{'open_time':at,'price':bars[-1].close,'available_at':at+1}}
    records=observer.process(key,symbol,current,observed_at=at+2)
    entry=next(r for r in records if r['payload']['provenance'].get('research_entry'))
    assert entry['evidence_class']=='CONTEMPORANEOUS_CANDLE_OPEN' and entry['observed_at']-entry['decision_time']==2
    r=registration(key);inst={'instance_id':r['instance_id'],'parameter_set_fingerprint':r['parameter_hash'],'orchestrator_run_id':config.run_id,'publication_activation_time':config.activation_time,'active_instruments':[symbol],'execution_mode':'OFF','status':'RESEARCH','signal_publication':True}
    signal=StrategyLibraryAdapter(MagicMock(),key,inst).convert(entry['event_id'],symbol,entry)
    assert signal.strategy_version==d.version and signal.strategy_metadata['signal_emission_class']=='PROSPECTIVE'
    assert signal.strategy_metadata['research_only'] is True and signal.strategy_metadata['execution_eligible'] is False
    from execution_v2.eligibility import research_restricted
    assert research_restricted(signal.strategy_metadata, signal.provenance)
    restored=ForwardObserver(config,artifact='frozen-test');restored.restore(observer.state)
    repeated=restored.process(key,symbol,current,observed_at=at+3)
    assert not any(r.get('payload',{}).get('provenance',{}).get('research_entry') for r in repeated)


def test_original_canary_signal_payloads_remain_identical_to_pre_expansion_adapter():
    from dataclasses import asdict
    from tests.test_strategy_library_orchestration import fixture
    expected={'donchian_breakout': '3b1742e73b575e39e0ab941e753f7521c949f36de71b306a96677b378981d640', 'ema_pullback': 'd46ae0b873fd449be5903dd34fcf23e0d1ed38328fa230c8d6dd496ffb3207d0', 'fvg_retracement': '90ed7626cfdfb815634e91c0a1eb503d3f6b59723e7f3f5791ed4db27e8cd891'}
    for key,expected_hash in expected.items():
        instance,record=fixture(key)
        signal=StrategyLibraryAdapter(MagicMock(),key,instance).convert('event-1','XAUUSD',record)
        assert canonical_hash(asdict(signal))==expected_hash
