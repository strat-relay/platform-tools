from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from core.strategies.entry_quote import capture_entry_quote
from research.strategy_library_forward import ForwardObserver, RunConfig, run_once
from tests.test_strategy_library_forward import LibraryForwardTests, cached
from tests.test_strategy_library_orchestration import fixture
from orchestration.adapters.strategy_library import StrategyLibraryAdapter
from strategy_backtest.library import BY_KEY


def quote(at, price=100):
    return {'quote': {'time': at+1, 'bid': price-.01, 'ask': price+.02}, 'observed_at': at+1.5}


@pytest.mark.parametrize('direction,side,price', [('LONG','ASK',100.02), ('SHORT','BID',99.99)])
def test_tentative_entry_uses_actual_side_and_preserves_all_quote_times(direction, side, price):
    q = capture_entry_quote(quote(900), instrument='XAUUSD', provider_symbol='XAUUSDm',
        direction=direction, entry_time=900, observed_at=902, max_age_seconds=5)
    assert q['tentative_entry_price'] == price and q['price_side'] == side
    assert q['provider_timestamp'] == 901 and q['cache_observed_at'] == 901.5
    assert q['observed_at'] == 902 and q['quote_age_seconds'] == 1


@pytest.mark.parametrize('change', [
    {'bid': True}, {'ask': float('nan')}, {'bid': 101}, {'time':899},
    {'time':903}, {'time':890}, {'symbol':'BTCUSDm'},
])
def test_invalid_or_pre_entry_quotes_never_become_tentative_prices(change):
    raw = quote(900); raw['quote'].update(change)
    with pytest.raises(ValueError):
        capture_entry_quote(raw, instrument='XAUUSD', provider_symbol='XAUUSDm',
            direction='LONG', entry_time=900, observed_at=902, max_age_seconds=5)


def observer():
    helper=LibraryForwardTests(); old,d,bars,context,symbol=helper.observer()
    cfg=replace(old.config, run_id='fresh-quote-run', entry_mode='CONTEMPORANEOUS_CANDLE_OPEN',
                entry_quote_max_age_seconds=5)
    obs=ForwardObserver(cfg,artifact='new-artifact')
    helper.bootstrap(obs,d,bars,context,symbol)
    at=bars[-1].close_timestamp
    view=cached(d,bars,context,at+2)
    view.opening_observations={d.execution:{'open_time':at,'price':bars[-1].close,'available_at':at+1}}
    view.entry_quote=quote(at,bars[-1].close)
    return obs,d,bars,context,symbol,view,at


def test_quote_is_frozen_before_entry_candle_exists_and_survives_canonical_conversion():
    obs,d,bars,context,symbol,view,at=observer()
    records=obs.process(d.key,symbol,view,observed_at=at+2)
    accepted=next(r for r in records if r['payload'].get('provenance',{}).get('research_entry'))
    q=deepcopy(accepted['payload']['provenance']['entry_quote'])
    inst,_=fixture();inst.update(active_instruments=[symbol],publication_activation_time=obs.config.activation_time)
    signal=StrategyLibraryAdapter(MagicMock(),d.key,inst).convert(accepted['event_id'],symbol,accepted)
    assert signal.strategy_metadata['entry_quote']==q
    assert signal.entry_price==bars[-1].close  # explicit theoretical provider-open basis
    assert q['tentative_entry_price']==view.entry_quote['quote']['ask']
    view.entry_quote['quote']['ask']+=100
    restored=ForwardObserver(obs.config,artifact='new-artifact');restored.restore(obs.state)
    assert not any(r.get('payload',{}).get('provenance',{}).get('research_entry')
                   for r in restored.process(d.key,symbol,view,observed_at=at+3))
    assert accepted['payload']['provenance']['entry_quote']==q


def test_missing_quote_is_atomic_then_can_retry_with_real_quote():
    obs,d,bars,context,symbol,view,at=observer()
    saved=deepcopy(obs.state);raw=view.entry_quote;view.entry_quote=None
    with pytest.raises(ValueError,match='quote unavailable'):
        obs.process(d.key,symbol,view,observed_at=at+2)
    assert obs.state==saved
    view.entry_quote=raw
    assert any(r['payload'].get('provenance',{}).get('research_entry')
               for r in obs.process(d.key,symbol,view,observed_at=at+3))


def test_quote_contract_cannot_be_enabled_on_completed_candle_reconstruction():
    with pytest.raises(ValueError,match='next-open mode'):
        RunConfig('bad',900,('donchian_breakout',),{'XAUUSD':'XAUUSDm'},entry_quote_max_age_seconds=5)


@pytest.mark.parametrize('key', [k for k,d in BY_KEY.items() if d.execution=='M15'])
def test_every_m15_library_composition_observes_quote_without_waiting_for_entry_close(key):
    helper=LibraryForwardTests();old,d,bars,context,symbol=helper.observer(key)
    cfg=replace(old.config,run_id='quote-'+key,entry_mode='CONTEMPORANEOUS_CANDLE_OPEN',entry_quote_max_age_seconds=5)
    obs=ForwardObserver(cfg,artifact='new');helper.bootstrap(obs,d,bars,context,symbol)
    at=bars[-1].close_timestamp;view=cached(d,bars,context,at+2)
    view.opening_observations={d.execution:{'open_time':at,'price':bars[-1].close,'available_at':at+1}}
    view.entry_quote=quote(at,bars[-1].close)
    records=obs.process(key,symbol,view,observed_at=at+2)
    entry=next(r for r in records if r['payload'].get('provenance',{}).get('research_entry'))
    assert entry['decision_time']==at and entry['observed_at']==at+2
    assert entry['payload']['provenance']['entry_quote']['bid']==view.entry_quote['quote']['bid']


def test_repeated_discovery_does_not_change_canonical_signal_identity():
    inst,record=fixture();record['stage_timing']={'evaluation_completed_at':record['observed_at']+.1}
    adapter=StrategyLibraryAdapter(MagicMock(),'donchian_breakout',inst)
    adapter.conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[('event','XAUUSD',record)]
    first=adapter.discover_new_signals(set())[0]
    second=adapter.discover_new_signals(set())[0]
    assert first.to_dict()==second.to_dict()


def test_poll_cadence_counts_processing_time_and_keeps_cycles_sequential():
    from research.strategy_library_forward import main
    cfg=RunConfig('test',900,('donchian_breakout',),{'XAUUSD':'XAUUSDm'})
    with patch('sys.argv',['observer','run','--manifest','unused','--interval','1']), \
         patch.object(RunConfig,'load',return_value=cfg), \
         patch('research.strategy_library_forward.run_once',return_value={}) as once, \
         patch('research.strategy_library_forward.time.monotonic',side_effect=[100,100.4]), \
         patch('research.strategy_library_forward.time.sleep',side_effect=KeyboardInterrupt) as sleep:
        with pytest.raises(KeyboardInterrupt):main()
    once.assert_called_once();assert sleep.call_args.args[0]==pytest.approx(.6)


def test_batch_logs_commit_only_after_transaction_context_exits(capsys):
    obs,d,bars,context,symbol,view,at=observer()
    conn=MagicMock();ledger=MagicMock();ledger.lock_and_load.return_value=obs.state
    reader=MagicMock();reader.get_snapshot.return_value=view
    with patch('research.strategy_library_forward.ForwardObserver',return_value=obs), \
         patch('research.strategy_library_forward.PostgresResearchLedger',return_value=ledger), \
         patch('research.strategy_library_forward.CanonicalMarketDataReader',return_value=reader), \
         patch('research.strategy_library_forward.default_store'), \
         patch('research.strategy_library_forward.PostgresConfig'), \
         patch('research.strategy_library_forward.connect',return_value=conn), \
         patch('research.strategy_library_forward.time.time',return_value=at+2):
        run_once(obs.config)
    assert reader.get_snapshot.call_args.kwargs['include_entry_quote'] is True
    state,records=ledger.save.call_args.args
    assert records and all('evaluation_completed_at' in r['stage_timing'] for r in records)
    conn.__exit__.assert_called_once()
    assert 'transaction_committed_at' in capsys.readouterr().out


def test_failed_commit_does_not_claim_batch_committed(capsys):
    obs,d,bars,context,symbol,view,at=observer()
    conn=MagicMock();conn.__exit__.side_effect=RuntimeError('commit failed')
    ledger=MagicMock();ledger.lock_and_load.return_value=obs.state
    reader=MagicMock();reader.get_snapshot.return_value=view
    with patch('research.strategy_library_forward.ForwardObserver',return_value=obs), \
         patch('research.strategy_library_forward.PostgresResearchLedger',return_value=ledger), \
         patch('research.strategy_library_forward.CanonicalMarketDataReader',return_value=reader), \
         patch('research.strategy_library_forward.default_store'), \
         patch('research.strategy_library_forward.PostgresConfig'), \
         patch('research.strategy_library_forward.connect',return_value=conn), \
         patch('research.strategy_library_forward.time.time',return_value=at+2):
        with pytest.raises(RuntimeError,match='commit failed'): run_once(obs.config)
    assert 'library_observer_batch_committed' not in capsys.readouterr().out
