from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from execution_v2.fakes import FakeConnection
from execution_v2.intent import create_execution_intent
from execution_v2.risk import RiskPolicy
from migration.signal import canonical_signal
from orchestration.adapters.strategy_library import StrategyLibraryAdapter
from orchestration.library_outcomes import resolve_library_signal
from outcome_resolver import Candle
from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverSignal
from platform_api.library_observation import observation_status
from platform_api.control import PlatformControlApi
from research.strategy_library_registration import CANARY, registration
from signal_orchestrator import live_classification, load_adapters


T = 1792175400


def fixture(key='donchian_breakout'):
    r = registration(key)
    inst = {'instance_id':r['instance_id'], 'parameter_set_fingerprint':r['parameter_hash'],
            'orchestrator_run_id':'library-canary-20261016-v1', 'publication_activation_time':T-900,
            'active_instruments':['XAUUSD'], 'enabled':True, 'strategy_id':r['strategy_id']}
    entry = {'episode_id':'episode1', 'strategy_version':r['strategy_id']+'@V1',
             'instrument':'XAUUSD','direction':'LONG', 'entry_timestamp':T,
             'entry_price':100, 'stop_price':99,'target_price':102,
             'parameter_hash':r['parameter_hash'], 'provenance':{'entry_cancelled':False}}
    record = {'execution_eligible':False,'observed_at':T+901,
              'strategy_key':key, 'instrument':'XAUUSD', 'decision_time':T+900,
              'evidence_class':'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION',
              'payload':{'strategy_id':r['strategy_id'], 'parameter_set_id':r['parameter_set_id'],
                         'provenance':{'research_only':True,'execution_eligible':False,
                          'reason':'ENTRY_ACCEPTED',
                          'parameter_hash':r['parameter_hash'], 'information_cutoff':T+900,
                          'research_entry':entry}}}
    return inst, record


class LibraryOrchestrationTests(unittest.TestCase):
    def test_actual_frozen_observer_entry_converts_without_changing_the_evaluator(self):
        from tests.test_strategy_library_forward import LibraryForwardTests, cached
        helper=LibraryForwardTests()
        observer,d,bars,context,symbol=helper.observer()
        helper.bootstrap(observer,d,bars,context,symbol)
        observer.process(d.key,symbol,cached(d,bars,context,bars[-1].close_timestamp+1),
                         observed_at=bars[-1].close_timestamp+1)
        start=bars[-1].close_timestamp
        candle=replace(bars[-1],open_timestamp=start,close_timestamp=start+900,
                       open=bars[-1].close,high=bars[-1].close+.1,
                       low=bars[-1].close-.1,close=bars[-1].close)
        records=observer.process(d.key,symbol,cached(d,bars+(candle,),context,start+901),observed_at=start+901)
        accepted=next(r for r in records if r['kind']=='EVALUATION' and r['payload']['provenance'].get('research_entry'))
        instance,_=fixture()
        instance.update(active_instruments=[symbol],publication_activation_time=observer.config.activation_time)
        signal=StrategyLibraryAdapter(MagicMock(),d.key,instance).convert(accepted['event_id'],symbol,accepted)
        self.assertEqual(signal.strategy_metadata['artifact_digest'],accepted['payload']['provenance']['artifact_digest'])
        self.assertEqual(signal.entry_price,candle.open)

    def adapter(self, key='donchian_breakout'):
        instance, record = fixture(key)
        return StrategyLibraryAdapter(MagicMock(),key,instance), record

    def test_all_canary_signals_preserve_provenance_and_explicit_parameters(self):
        for key in CANARY:
            with self.subTest(key=key):
                adapter, record = self.adapter(key)
                signal = adapter.convert('event','XAUUSD',record)
                canonical = canonical_signal(signal.to_dict(), source_reference={'source_id':'test'})
                self.assertEqual(canonical.fields['parameter_set_ref'],registration(key)['parameter_set_id'])
                self.assertEqual(canonical.fields['parameter_set_status'],'EXPLICIT')
                self.assertEqual(signal.strategy_metadata['signal_emission_class'],'RETROSPECTIVE_RESEARCH_EVIDENCE')
                self.assertFalse(signal.strategy_metadata['execution_eligible'])
                self.assertEqual(signal.signal_timestamp,datetime.fromtimestamp(T,timezone.utc).isoformat().replace('+00:00','Z'))
                self.assertEqual(signal.strategy_metadata['outcome_contract']['max_hold_minutes'],None)
                self.assertEqual(live_classification(signal.to_dict(),{'live_execution_enabled':True,
                                  'live_execution_cutoff_timestamp':'2020-01-01T00:00:00Z'}),'PRE_LIVE_EXECUTION')

    def test_invalid_provenance_or_geometry_never_becomes_a_signal(self):
        adapter, good = self.adapter()
        for field, value in [('instrument','BTCUSD'),('entry_price',float('nan')),
                             ('stop_price',101),('parameter_hash','wrong')]:
            bad=deepcopy(good);bad['payload']['provenance']['research_entry'][field]=value
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapter.convert('event','XAUUSD',bad)
        for observed in (T+899,float('inf')):
            bad=deepcopy(good);bad['observed_at']=observed
            with self.assertRaises(ValueError): adapter.convert('event','XAUUSD',bad)

    def test_episode_identity_survives_duplicate_event_replay(self):
        adapter, record = self.adapter()
        first=adapter.convert('event1','XAUUSD',record)
        self.assertEqual(first.signal_id,adapter.convert('event2','XAUUSD',record).signal_id)
        adapter.conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[('event1','XAUUSD',record)]
        self.assertEqual(adapter.discover_new_signals({first.signal_id}),[])
        self.assertEqual(len(adapter.discover_new_signals(set())),1)

    def test_unregistered_or_wrong_parameter_identity_fails_closed(self):
        instance,_=fixture();instance['parameter_set_fingerprint']='bad'
        with self.assertRaises(ValueError): StrategyLibraryAdapter(None,'donchian_breakout',instance)
        with self.assertRaises(ValueError): StrategyLibraryAdapter(None,'ema_momentum',instance)

    def test_only_enabled_parent_and_instance_load_the_adapter(self):
        instance,_=fixture()
        config={'strategies':[{'strategy_id':instance['strategy_id'],'enabled':True,
                              'adapter':'StrategyLibraryAdapter'}], 'instances':[instance]}
        self.assertEqual(len(load_adapters(config,'2026-01-01T00:00:00Z',MagicMock())),1)
        config['instances'][0]['enabled']=False
        self.assertEqual(load_adapters(config,'2026-01-01T00:00:00Z',MagicMock()),[])
        config['strategies'][0]['enabled']=False
        self.assertEqual(load_adapters(config,'2026-01-01T00:00:00Z',MagicMock()),[])

    def test_execution_intents_blocked_even_with_permissive_risk(self):
        for key in CANARY:
            conn=FakeConnection();adapter,record=self.adapter(key)
            signal=adapter.convert('event','XAUUSD',record)
            conn.seed_entry_signal(signal_id=signal.signal_id,strategy_id=signal.strategy_id,
                                   entry_signal_hash='hash')
            policy=RiskPolicy(version=1,enabled=True,max_volume=.01,allowed_accounts=('a',),
                              allowed_symbols=None,max_signal_age_seconds=999999,source='test')
            result=create_execution_intent(conn,signal_id=signal.signal_id,account_id='a',
                                           risk_policy=policy,now_utc=datetime.now(timezone.utc))
            self.assertFalse(result.eligible)
            self.assertIsNone(result.execution_intent_id)
            self.assertEqual(conn.tables.get('execution_v2.execution_intent',{}),{})


class LibraryOutcomeTests(unittest.TestCase):
    def signal(self):
        return ResolverSignal('s','BREAKOUT_DONCHIAN_BREAKOUT','XAUUSD','LONG',100,99,102,
                              datetime.fromtimestamp(T,timezone.utc),'MARKET_PAPER_OBSERVATION',
                              {'outcome_contract':{'version':'library-research-outcome.v1'}},{})

    def candle(self, index=0, high=103,low=99.5,opening=100):
        start=datetime.fromtimestamp(T+index*900,timezone.utc)
        return Candle(start,start+timedelta(minutes=15),high,low,100,open=opening)

    def test_target_and_ambiguity_match_shared_policy(self):
        result=resolve_library_signal(self.signal(),[self.candle()],15)
        self.assertEqual((result.status,result.realized_r),('TARGET_HIT',2))
        ambiguous=resolve_library_signal(self.signal(),[self.candle(low=98)],15)
        self.assertEqual(ambiguous.resolution_state,'AMBIGUOUS_INTRABAR')
        self.assertIsNone(ambiguous.realized_r)

    def test_open_precedence_and_gap_valuation_are_preserved(self):
        bars=[self.candle(high=101),self.candle(2,high=104,low=98,opening=103)]
        result=resolve_library_signal(self.signal(),bars,15)
        self.assertEqual((result.status,result.realized_r),('TARGET_HIT',3))
        self.assertEqual(result.evidence['valuation_status'],'UNCERTAIN_GAP_OPEN_REFERENCE')
        gap=resolve_library_signal(self.signal(),[bars[0],replace(bars[1],open=100)],15)
        self.assertEqual(gap.resolution_state,'INSUFFICIENT_DATA')
        self.assertEqual(gap.status,'OPEN')
        self.assertIsNone(gap.realized_r)
        self.assertEqual(gap.evidence['status'],'DATA_GAP_UNRESOLVED')
        self.assertEqual(gap.evidence['reason'],'CANDLE_GAP')
        self.assertTrue(gap.evidence['unprocessed_path'])

    def test_missing_open_does_not_invent_a_price(self):
        result=resolve_library_signal(self.signal(),[replace(self.candle(),open=None)],15)
        self.assertEqual(result.resolution_state,'INSUFFICIENT_DATA')

    def test_missing_entry_candle_keeps_research_gap_evidence(self):
        for bars in ([], [self.candle(1)]):
            with self.subTest(bars=bars):
                result=resolve_library_signal(self.signal(),bars,15)
                self.assertEqual((result.status,result.resolution_state),('OPEN','INSUFFICIENT_DATA'))
                self.assertEqual(result.evidence['resolution_reason'],'ENTRY_CANDLE_UNAVAILABLE')
                self.assertTrue(result.evidence['unprocessed_path'])
                self.assertIsNone(result.exit_timestamp)

    def test_runtime_gap_writes_valid_state_without_advancing_cursor(self):
        runtime=OutcomeResolverRuntime(conn=MagicMock(),candle_store=MagicMock())
        runtime.candle_store.candles.return_value=[self.candle(high=101),self.candle(2,high=101)]
        runtime._cursor=lambda _:self.candle(2).close_timestamp
        runtime._prior_resolution=lambda _:('INSUFFICIENT_DATA',{})
        runtime._authority_lock=lambda _:None
        runtime._assert_lease=lambda _:None
        runtime._persist_state=MagicMock()
        with patch('outcome_resolver_runtime.persist_outcome_row') as persist:
            runtime.resolve_one(self.signal())
        self.assertEqual(persist.call_args.kwargs['resolution_state'],'INSUFFICIENT_DATA')
        self.assertEqual(runtime._persist_state.call_args.args[3],[])
        self.assertIsNone(runtime._persist_state.call_args.kwargs['prior_cursor'])

    def test_unified_runtime_dispatches_the_frozen_policy(self):
        runtime=OutcomeResolverRuntime(conn=MagicMock(),candle_store=MagicMock())
        runtime.candle_store.candles.return_value=[self.candle()]
        runtime._cursor=lambda _:None
        runtime._authority_lock=lambda _:None
        runtime._assert_lease=lambda _:None
        runtime._persist_state=MagicMock()
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result=runtime.resolve_one(self.signal())
        self.assertEqual(result.status,'TARGET_HIT')
        self.assertEqual(result.resolution_method,'LIBRARY_SHARED_RESEARCH_POLICY_V1')


class ObserverStatusTests(unittest.TestCase):
    def test_registry_aliases_do_not_create_duplicate_strategy_identities(self):
        for key in CANARY:
            r=registration(key)
            row={'family_key':r['catalog_key'],'instance_id':r['instance_id'],
                 'name':r['name'],'version_label':'V1','instruments':[],
                 'attributes':{},'online':False,'execution_eligible':False}
            summary=PlatformControlApi._registry_summary([row])[0]
            self.assertEqual(summary['strategy_id'],r['strategy_id'])
            self.assertEqual(summary['instances'][0]['instance_id'],r['instance_id'])

    def test_never_calls_configured_online_a_healthy_observer(self):
        self.assertEqual(observation_status(None,'k',now=T)['status'],'NOT_OBSERVED')
        state={'health':{'observed_at':T,'streams':[{'key':'k','status':'OK','last_finalized_close':T}]}}
        self.assertEqual(observation_status(state,'k',now=T+30)['status'],'RUNNING')
        self.assertEqual(observation_status(state,'k',now=T+331)['status'],'STALE')
        self.assertEqual(observation_status(state,'other',now=T+30)['status'],'DEGRADED')
