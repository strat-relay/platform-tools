"""Causal event processing and crash/ack safety; no broker or live NATS calls."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from core.strategies.evaluation import canonical_hash
from research.library_event_consumer import process_market, transition
from research.library_event_service import handle_message, relay_batch
from research.library_event_store import MARKET, ENTRY, SCHEMA
from research.strategy_library_forward import ForwardObserver
from tests.test_strategy_library_forward import LibraryForwardTests, cached


def frames(provider=None):
    helper=LibraryForwardTests();old,d,bars,context,symbol=helper.observer()
    provider=provider or symbol+'m'
    cfg=replace(old.config,run_id='fresh-event-test',symbols={symbol:provider},
        transport='JETSTREAM',entry_mode='CONTEMPORANEOUS_CANDLE_OPEN',entry_quote_max_age_seconds=5)
    output=[]
    for seq,selected in enumerate((bars[:-1],bars),1):
        at=selected[-1].close_timestamp;now=at+2
        cached_view=cached(d,selected,context,now)
        series={}
        for tf,data in cached_view.bars.items():
            rows=data.bars;first=rows[0];step=900 if tf=='M15' else 3600
            padding=[{**first,'time':first['time']-step*i} for i in range(1,321-len(rows))]
            series[tf]=list(reversed(padding))+rows
        remainder={'source_read_health':True,'symbol_info':{'tick_size':.01},
            'opening_observations':{d.execution:{'open_time':at,'price':selected[-1].close,'available_at':at+1}}}
        snapshot={'provider_symbol':provider,'observed_at':now,'snapshot':remainder,'bars':series}
        quote={'quote':{'time':at+1,'bid':selected[-1].close-.01,'ask':selected[-1].close+.01},'observed_at':at+1.5}
        event={'schema':SCHEMA,'provider_symbol':provider,'sequence':seq,
            'snapshot_id':canonical_hash(snapshot),'observed_at':now,'quote':quote,'kind':'SNAPSHOT'}
        output.append((event,snapshot,now))
    return cfg,d,symbol,output


class MemoryStore:
    """Transactional test transport storage; strategy logic remains the real evaluator."""
    def __init__(self,cfg,items):
        self.events={canonical_hash(e):deepcopy(e) for e,_,_ in items}
        self.snapshots={canonical_hash(s):s for _,s,_ in items}
        self.state=ForwardObserver(cfg).state
        self.state['activation_attestation']={'schema':SCHEMA,'run_id':cfg.run_id,'binding':self.state['binding'],
            'activation_time':cfg.activation_time,'transport':'JETSTREAM','execution_eligible':False}
        self.cursors={};self.inbox={};self.records={};self.notifications={};self.fail_save=False
        self.conn=MagicMock()

    @contextmanager
    def atomic(self,_):
        before=deepcopy((self.state,self.cursors,self.inbox,self.records,self.notifications))
        try:yield
        except Exception:
            self.state,self.cursors,self.inbox,self.records,self.notifications=before
            raise

    def event(self,event_id,subject):return deepcopy(self.events[event_id])
    def seen(self,consumer,event_id):return (consumer,event_id) in self.inbox
    def done(self,consumer,event_id,result):self.inbox[consumer,event_id]=result
    def snapshot(self,snapshot_id,provider):return deepcopy(self.snapshots[snapshot_id])
    def cursor(self,consumer,provider):return self.cursors.get((consumer,provider),0)
    def advance_cursor(self,consumer,provider,seq):self.cursors[consumer,provider]=seq
    def lock_and_load(self):return deepcopy(self.state)
    def save(self,state,records):
        self.state=deepcopy(state)
        for r in records:self.records[r['event_id']]=deepcopy(r)
        if self.fail_save:raise RuntimeError('injected save failure')
    def append(self,subject,provider,payload,sequence=None):
        assert subject==ENTRY
        self.notifications[canonical_hash(payload)]=deepcopy(payload)


class LibraryEventTests(unittest.TestCase):
    def setUp(self):
        self.cfg,self.d,self.symbol,self.items=frames()
        self.store=MemoryStore(self.cfg,self.items)

    def process(self,index,now=None):
        event,_,at=self.items[index]
        with patch('research.library_event_consumer.EventStore',return_value=self.store), \
             patch('research.library_event_consumer.PostgresResearchLedger',return_value=self.store):
            return process_market(self.store.conn,self.cfg,canonical_hash(event),now=at if now is None else now)

    def test_next_open_entry_and_notification_without_entry_candle(self):
        self.process(0);self.process(1)
        self.assertEqual(len(self.store.notifications),1)
        entry=next(r for r in self.store.records.values() if r['payload'].get('provenance',{}).get('research_entry'))
        self.assertEqual(entry['evidence_class'],'CONTEMPORANEOUS_CANDLE_OPEN')
        self.assertEqual(entry['observed_at']-entry['decision_time'],2)
        self.assertIn('entry_quote',entry['payload']['provenance'])
        self.assertEqual(self.process(1),'DUPLICATE')
        self.assertEqual(len(self.store.notifications),1)

    def test_save_crash_rolls_back_cursor_entry_and_notification_then_retries(self):
        self.process(0);before=deepcopy((self.store.state,self.store.cursors,self.store.records))
        self.store.fail_save=True
        with self.assertRaises(RuntimeError):self.process(1)
        self.assertEqual((self.store.state,self.store.cursors,self.store.records),before)
        self.assertFalse(self.store.notifications)
        self.store.fail_save=False;self.process(1)
        self.assertEqual(len(self.store.notifications),1)

    def test_out_of_order_event_is_not_consumed(self):
        with self.assertRaisesRegex(ValueError,'sequence gap'):self.process(1)
        self.assertFalse(self.store.inbox);self.assertFalse(self.store.cursors)

    def test_stale_quote_is_acknowledgeable_without_losing_pending_setup(self):
        self.process(0)
        old_id=canonical_hash(self.items[1][0]);self.store.events[old_id]['quote']['quote']['time']-=30
        self.assertEqual(self.process(1),'WAITING_QUOTE')
        self.assertFalse(self.store.notifications)
        event,snapshot,now=self.items[1]
        event={**event,'sequence':3,'observed_at':now+1};event_id=canonical_hash(event)
        self.store.events[event_id]=event
        with patch('research.library_event_consumer.EventStore',return_value=self.store), \
             patch('research.library_event_consumer.PostgresResearchLedger',return_value=self.store):
            process_market(self.store.conn,self.cfg,event_id,now=now+1)
        self.assertEqual(len(self.store.notifications),1)

    def test_delayed_delivery_never_replays_a_prospective_open(self):
        self.process(0);self.process(1,now=self.items[1][2]+120)
        self.assertFalse(self.store.notifications)
        self.assertTrue(self.store.state['health']['recovery'])

    def test_missing_h1_watermark_buffers_entry_until_context_arrives(self):
        self.process(0)
        event,snapshot,now=self.items[1]
        at=now-2
        if snapshot['bars']['H1'][-1]['time']+3600 != at:
            self.skipTest('fixture does not land on H1 boundary')
        observer=ForwardObserver(self.cfg);observer.restore(self.store.state)
        partial=deepcopy(snapshot);partial['bars']['H1']=partial['bars']['H1'][:-1]
        # Preserve history length while withholding the new boundary.
        first=partial['bars']['H1'][0];partial['bars']['H1'].insert(0,{**first,'time':first['time']-3600})
        records,_=transition(observer,event,partial,now=now)
        self.assertFalse(any(r['payload'].get('provenance',{}).get('research_entry') for r in records))
        records,_=transition(observer,event,snapshot,now=now+1)
        self.assertTrue(any(r['payload'].get('provenance',{}).get('research_entry') for r in records))

    def test_snapshot_future_bar_cannot_leak_into_late_replay(self):
        event,snapshot,now=self.items[0];snapshot=deepcopy(snapshot)
        snapshot['bars']['M15'][-1]['time']+=900
        with self.assertRaisesRegex(ValueError,'original source observation'):
            transition(ForwardObserver(self.cfg),event,snapshot,now=now+1800)

    def test_ack_occurs_only_after_effects_finish(self):
        calls=[];msg=SimpleNamespace(subject=MARKET,data=json.dumps({'event_id':'a'*64}).encode(),
            ack_sync=AsyncMock(side_effect=lambda:calls.append('ack')),nak=AsyncMock(),in_progress=AsyncMock())
        def handler(event_id,at):calls.append('commit');return 'PROCESSED'
        asyncio.run(handle_message(msg,handler,subject=MARKET))
        self.assertEqual(calls,['commit','ack']);msg.nak.assert_not_called()

    def test_failed_effect_is_nacked_without_ack(self):
        msg=SimpleNamespace(subject=MARKET,data=json.dumps({'event_id':'a'*64}).encode(),
            ack_sync=AsyncMock(),nak=AsyncMock(),in_progress=AsyncMock())
        def handler(*_):raise RuntimeError('commit failed')
        asyncio.run(handle_message(msg,handler,subject=MARKET))
        msg.ack_sync.assert_not_called();msg.nak.assert_awaited_once()

    def test_lost_ack_redelivery_reuses_committed_effect(self):
        msg=SimpleNamespace(subject=MARKET,data=json.dumps({'event_id':canonical_hash(self.items[0][0])}).encode(),
            ack_sync=AsyncMock(side_effect=ConnectionError),nak=AsyncMock(),in_progress=AsyncMock())
        asyncio.run(handle_message(msg,lambda *_:self.process(0),subject=MARKET))
        msg.ack_sync=AsyncMock()
        asyncio.run(handle_message(msg,lambda *_:self.process(0),subject=MARKET))
        self.assertEqual(len(self.store.inbox),1)

    def test_failed_jetstream_publish_rolls_back_outbox_bookkeeping(self):
        store=MagicMock();store.atomic.return_value.__enter__.return_value=None
        store.pending.return_value=[('a'*64,MARKET)]
        store.event.return_value={'provider_symbol':'BTCUSDm'}
        js=SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError))
        with patch('research.library_event_service.EventStore',return_value=store):
            with self.assertRaises(ConnectionError):asyncio.run(relay_batch(self.store.conn,js))
        self.store.conn.cursor.assert_not_called()

    def test_research_notifications_never_enter_execution_stream(self):
        from infrastructure.messaging.contracts import STREAMS
        self.assertNotIn(ENTRY,STREAMS['EXECUTION']['subjects'])
        self.assertNotIn(ENTRY,STREAMS['TRADING_CORE']['subjects'])
        self.assertEqual(STREAMS['LIBRARY_RESEARCH']['subjects'],(ENTRY+'.*',))

    def test_legacy_scan_is_disabled_for_explicit_event_owned_instance(self):
        from tests.test_strategy_library_orchestration import fixture
        from orchestration.adapters.strategy_library import StrategyLibraryAdapter
        instance,_=fixture();instance['publication_transport']='JETSTREAM'
        conn=MagicMock();adapter=StrategyLibraryAdapter(conn,'donchian_breakout',instance)
        self.assertEqual(adapter.discover_new_signals(set()),[])
        conn.cursor.assert_not_called()

    def dispatch_fixture(self):
        from research.strategy_library_registration import registration
        self.process(0);self.process(1)
        event_id,event=next(iter(self.store.notifications.items()))
        self.store.events[event_id]=event
        record=self.store.records[event['record_id']]
        identity=registration(event['strategy_key'])
        attrs={'status':'RESEARCH','signal_publication':True,'publication_transport':'JETSTREAM',
            'orchestrator_run_id':self.cfg.run_id,'publication_activation_time':self.cfg.activation_time,
            'library_key':event['strategy_key']}
        managed=(True,False,'OFF',attrs,identity['parameter_hash'],identity['parameters']['version'],
                 True,identity['parameters'],identity['evaluator_key'])
        cur=self.store.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect=[(record,),managed,(True,'StrategyLibraryAdapter'),None]
        cur.fetchall.return_value=[(self.symbol,)]
        return event_id,event,cur,managed

    def test_orchestrator_dispatch_uses_canonical_publisher_and_stale_delivery_stays_research(self):
        from research.library_event_consumer import dispatch_research
        event_id,event,cur,managed=self.dispatch_fixture();publisher=MagicMock()
        now=self.items[1][2]+120
        with patch('research.library_event_consumer.EventStore',return_value=self.store):
            result=dispatch_research(self.store.conn,self.cfg,event_id,publisher,now=now)
        self.assertEqual(result,'PUBLISHED_RESEARCH')
        signal=publisher.publish.call_args.args[0]
        self.assertFalse(signal.strategy_metadata['execution_eligible'])
        self.assertEqual(signal.strategy_metadata['signal_emission_class'],'RETROSPECTIVE_RESEARCH_EVIDENCE')
        self.assertEqual(signal.strategy_metadata['timing_rejection_reason'],'ENTRY_DISPATCH_WINDOW_EXPIRED')

    def test_live_managed_instance_cannot_use_research_event_consumer(self):
        from research.library_event_consumer import dispatch_research
        event_id,event,cur,managed=self.dispatch_fixture();publisher=MagicMock()
        managed=list(managed);managed[1]=True;managed[2]='LIVE'
        record=self.store.records[event['record_id']]
        cur.fetchone.side_effect=[(record,),tuple(managed),(True,'StrategyLibraryAdapter')]
        with patch('research.library_event_consumer.EventStore',return_value=self.store):
            with self.assertRaisesRegex(ValueError,'publication gate'):
                dispatch_research(self.store.conn,self.cfg,event_id,publisher,now=self.items[1][2])
        publisher.publish.assert_not_called()

    def test_publication_retry_preserves_original_canonical_classification(self):
        from research.library_event_consumer import dispatch_research
        from orchestration.adapters.strategy_library import StrategyLibraryAdapter
        from research.strategy_library_registration import registration
        event_id,event,cur,managed=self.dispatch_fixture();publisher=MagicMock()
        instance={**managed[3],'instance_id':registration(event['strategy_key'])['instance_id'],
            'parameter_set_fingerprint':managed[4],'execution_mode':'OFF','active_instruments':[self.symbol]}
        record=self.store.records[event['record_id']]
        signal=StrategyLibraryAdapter(self.store.conn,event['strategy_key'],instance).convert(event['record_id'],self.symbol,record)
        cur.fetchone.side_effect=[(record,),managed,(True,'StrategyLibraryAdapter'),
                                 (signal.strategy_id,signal.source_event_id,signal.strategy_instance_id)]
        with patch('research.library_event_consumer.EventStore',return_value=self.store):
            result=dispatch_research(self.store.conn,self.cfg,event_id,publisher,now=self.items[1][2]+120)
        self.assertEqual(result,'DUPLICATE_CANONICAL');publisher.publish.assert_not_called()

    def test_quote_events_do_not_rerun_settled_strategy(self):
        self.process(0);self.process(1)
        event,snapshot,now=self.items[1];event={**event,'sequence':3,'kind':'QUOTE','observed_at':now+1}
        observer=ForwardObserver(self.cfg);observer.restore(self.store.state)
        with patch.object(observer,'process',side_effect=AssertionError('unnecessary evaluator work')):
            records,_=transition(observer,event,snapshot,now=now+1)
        self.assertEqual(records,[])

    def test_explicit_stream_provisioning_uses_bounded_durable_sdk_contract(self):
        try:
            from nats.js.errors import NotFoundError
        except ImportError:self.skipTest('nats-py unavailable')
        from research.library_event_service import provision_streams
        configs={}
        async def info(name):
            if name not in configs:raise NotFoundError
            return SimpleNamespace(config=configs[name])
        async def add(*,config):configs[config.name]=config
        js=SimpleNamespace(stream_info=info,add_stream=add)
        asyncio.run(provision_streams(js));asyncio.run(provision_streams(js))
        self.assertEqual(len(configs),2)
        self.assertTrue(all(c.num_replicas==1 and c.max_bytes==256*1024*1024 for c in configs.values()))

    def test_collector_event_publication_is_opt_in(self):
        from research.library_event_store import collector_sink_from_env
        with patch.dict('os.environ',{'MARKET_DATA_EVENT_MODE':'OFF'}), \
             patch('research.library_event_store.open_connection',side_effect=AssertionError('unexpected DB access')):
            self.assertIsNone(collector_sink_from_env())

    def test_cutover_plan_requires_future_boundary_and_preserves_old_evidence(self):
        from research.library_event_plan import plan
        image='ghcr.io/strat-relay/trading-platform-runtime@sha256:'+'a'*64
        bundle=plan('fresh-events',1800,image,('fvg_retracement',),now=1000)
        self.assertEqual(bundle['manifest']['transport'],'JETSTREAM')
        self.assertEqual(bundle['manifest']['entry_quote_max_age_seconds'],5)
        self.assertIn('NOT i.online',bundle['activation_sql'])
        self.assertNotIn('DELETE',bundle['activation_sql'])
        self.assertNotIn('ON CONFLICT',bundle['activation_sql'])
        with self.assertRaises(ValueError):plan('fresh-events',1500,image,('fvg_retracement',),now=1000)

    def test_durable_consumer_uses_explicit_ack_and_one_inflight_message(self):
        try:from nats.js.api import AckPolicy
        except ImportError:self.skipTest('nats-py unavailable')
        from research.library_event_service import consume
        async def run():
            stop=asyncio.Event()
            async def fetch(*_,**__):stop.set();return []
            sub=SimpleNamespace(fetch=fetch,unsubscribe=AsyncMock())
            js=SimpleNamespace(pull_subscribe=AsyncMock(return_value=sub))
            async def info(*_):return SimpleNamespace(config=js.pull_subscribe.call_args.kwargs['config'])
            js.consumer_info=info
            await consume(js,'observer',self.cfg,stop)
            options=js.pull_subscribe.call_args.kwargs['config']
            self.assertEqual(options.ack_policy,AckPolicy.EXPLICIT)
            self.assertEqual(options.max_ack_pending,1)
            self.assertEqual(len(options.filter_subjects),len(self.cfg.symbols))
            sub.unsubscribe.assert_awaited_once()
        asyncio.run(run())

    def test_collector_does_not_advance_cache_after_evidence_commit_failure(self):
        try:import fakeredis
        except ImportError:self.skipTest('fakeredis unavailable')
        from market_data_cache.collector import MarketDataCollector
        from market_data_cache.store import MarketDataStore
        from tests.test_market_data_cache import FakeBridge,START
        now=START+3;store=MarketDataStore(fakeredis.FakeRedis());sink=MagicMock()
        sink.snapshot.side_effect=RuntimeError('durable write failed')
        collector=MarketDataCollector(store,FakeBridge(lambda:now),bar_symbols=lambda:['XAUUSDm'],
                                      event_sink=sink,clock=lambda:now)
        with self.assertRaisesRegex(RuntimeError,'durable write'):
            collector._fetch('XAUUSDm',now,full=True,previous={})
        self.assertEqual(store.bars('XAUUSDm','M15'),[])
        self.assertIsNone(store.snapshot('XAUUSDm'))


if __name__=='__main__':unittest.main()
