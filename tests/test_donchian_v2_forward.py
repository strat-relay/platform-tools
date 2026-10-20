from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock
import unittest

from orchestration.adapters.strategy_library import StrategyLibraryAdapter
from research.donchian_v2_forward import KEY, WORKLOAD, SYMBOLS, identity, plan, registration_sql, verify
from research.replay_donchian_v2 import BASE, synthetic
from research.strategy_library_forward import ForwardObserver, RunConfig
from research.strategy_library_registration import registration
from strategy_backtest.library.forward_selection import forward_runtime
from strategy_backtest.library.donchian_v2 import DEFINITION, DonchianV2Runtime
from tests.test_strategy_library_forward import cached

IMAGE='ghcr.io/strat-relay/trading-platform-runtime@sha256:'+'a'*64


class DonchianForwardTests(unittest.TestCase):
    def observer(self):
        bars,context=synthetic()
        config=RunConfig('donchian-v2-test',BASE-1800,(KEY,),{'SYNTHETIC':'SYNTHETICm'},entry_mode='CONTEMPORANEOUS_CANDLE_OPEN')
        return ForwardObserver(config,artifact='frozen-test'),bars,context

    def test_forward_lifecycle_restart_and_orchestrator_provenance(self):
        observer,bars,context=self.observer()
        records=[]
        for i in range(39,len(bars)):
            now=bars[i].close_timestamp+1
            snapshot=cached(DEFINITION,bars[:i+1],context,now)
            result=observer.process(KEY,'SYNTHETIC',snapshot,observed_at=now)
            if i==39:self.assertEqual(result,[])
            records.extend(result)
            clone=ForwardObserver(observer.config,artifact='frozen-test')
            clone.restore(observer.state)
            repeated=clone.process(KEY,'SYNTHETIC',snapshot,observed_at=now)
            self.assertFalse(any(x['kind']=='EVALUATION' for x in repeated))
            self.assertTrue({x['event_id'] for x in repeated} <= {x['event_id'] for x in result})
        entries=[x for x in records if x['kind']=='EVALUATION' and x['payload']['provenance'].get('research_entry')]
        self.assertEqual(len(entries),2)
        self.assertTrue(all(x['evidence_class']=='FORWARD_COMPLETED_CANDLE_RECONSTRUCTION' for x in entries))
        row=identity()
        instance={'instance_id':row['instance_id'],'parameter_set_fingerprint':row['parameter_hash'],
                  'orchestrator_run_id':observer.config.run_id,'publication_activation_time':observer.config.activation_time,
                  'active_instruments':['SYNTHETIC'],'execution_mode':'OFF','status':'RESEARCH','signal_publication':True}
        adapter=StrategyLibraryAdapter(MagicMock(),KEY,instance)
        signal=adapter.convert(entries[0]['event_id'],'SYNTHETIC',entries[0])
        self.assertEqual(signal.strategy_version,'V2')
        self.assertEqual(signal.strategy_metadata['strategy_version'],'V2')
        self.assertEqual(signal.provenance['classification'],'RETROSPECTIVE_RESEARCH_EVIDENCE')
        self.assertFalse(signal.strategy_metadata['execution_eligible'])
        self.assertEqual(signal.strategy_instance_id,row['instance_id'])
        bad=deepcopy(entries[0]);bad['payload']['strategy_version']='V1'
        with self.assertRaises(ValueError):adapter.convert(bad['event_id'],'SYNTHETIC',bad)

    def test_live_provider_open_accepts_without_waiting_for_entry_candle_close(self):
        observer,bars,context=self.observer()
        for i in (39,40):
            now=bars[i].close_timestamp+1
            observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:i+1],context,now),observed_at=now)
        at=bars[41].close_timestamp;now=at+2
        snap=cached(DEFINITION,bars[:42],context,now)
        snap.opening_observations={'M15':{'open_time':at,'price':103.8,'available_at':at+1}}
        records=observer.process(KEY,'SYNTHETIC',snap,observed_at=now)
        accepted=[x for x in records if x['kind']=='EVALUATION' and x['payload']['provenance'].get('research_entry')]
        self.assertEqual(len(accepted),1)
        self.assertEqual(accepted[0]['evidence_class'],'CONTEMPORANEOUS_CANDLE_OPEN')
        self.assertEqual(accepted[0]['decision_time'],at)
        self.assertLess(accepted[0]['observed_at']-at,60)
        self.assertFalse(accepted[0]['execution_eligible'])

    def test_h1_watermark_wait_does_not_consume_m15_breakout(self):
        observer,bars,ctx=self.observer()
        now=bars[39].close_timestamp+1
        observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:40],ctx,now),observed_at=now)
        before=observer.state['streams'][KEY+':SYNTHETIC']['last_close']
        now=bars[40].close_timestamp+1
        delayed_ctx=tuple(x for x in ctx if x.close_timestamp<BASE)
        observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:41],delayed_ctx,now),observed_at=now)
        self.assertEqual(observer.state['streams'][KEY+':SYNTHETIC']['last_close'],before)
        observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:41],ctx,now+1),observed_at=now+1)
        episode=observer.state['streams'][KEY+':SYNTHETIC']['runtime']['state']['breakout_episodes']['SYNTHETIC']
        self.assertEqual(episode['phase'],'WAIT_RETEST')
        self.assertEqual(episode['break_at'],BASE)

    def test_bootstrap_cannot_keep_pre_activation_breakout(self):
        observer,bars,context=self.observer()
        # Starting on the breakout bar is warm-up, never a forward setup.
        now=bars[40].close_timestamp+1
        self.assertEqual(observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:41],context,now),observed_at=now),[])
        stream=observer.state['streams'][KEY+':SYNTHETIC']
        self.assertEqual(stream['runtime']['state']['breakout_episodes'],{})
        now=bars[41].close_timestamp+1
        events=observer.process(KEY,'SYNTHETIC',cached(DEFINITION,bars[:42],context,now),observed_at=now)
        self.assertFalse(any(e['payload']['provenance'].get('proposal') for e in events))

    def test_instance_detail_preserves_v2_under_shared_v1_parent(self):
        from platform_api.strategy_catalog import StrategyCatalogRepository
        repo=StrategyCatalogRepository(connect_fn=None)
        repo._run=lambda read:read(MagicMock())
        repo._instances=MagicMock(return_value=[{'instance_id':identity()['instance_id'],
            'strategy_id':'BREAKOUT_DONCHIAN_BREAKOUT','strategy_version':'V2','attributes':{}}])
        repo._rows=lambda cur,sql,args:([{'display_name':'Donchian','strategy_version':'V1',
            'trade_management':{}}] if 'SELECT display_name, strategy_version' in sql else [])
        repo._trade_management=MagicMock(return_value={})
        result=repo.instance_page('BREAKOUT_DONCHIAN_BREAKOUT',identity()['instance_id'])
        self.assertEqual(result['strategy_version'],'V2')

    def test_observation_detail_resolves_v2_without_legacy_aliases(self):
        from platform_api.strategy_observation import ObservationRepository
        r=identity();cur=MagicMock()
        cur.fetchall.return_value=[(r['instance_id'],r['evaluator_key'],{'library_key':KEY},
                                   ['BTCUSD'],r['parameter_set_id'],'LIBRARY:'+r['strategy_id'])]
        owner=ObservationRepository(None).owner(cur,r['strategy_id'],r['instance_id'])
        self.assertEqual(owner['strategy_id'],r['strategy_id'])
        self.assertEqual(owner['id'],r['instance_id'])

    def test_orchestrator_database_loader_recognizes_explicit_v2(self):
        from orchestration.config import _load_v2_instances_from_database
        r=identity();conn=MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value=[
            (r['instance_id'],r['evaluator_key'],'V2 observer',True,['BTCUSD'],[],
             {'library_key':KEY,'status':'RESEARCH'},r['parameter_hash'],None,'OFF',0)]
        rows=_load_v2_instances_from_database(conn)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['strategy_id'],'BREAKOUT_DONCHIAN_BREAKOUT')
        self.assertEqual(rows[0]['library_key'],KEY)
        self.assertEqual(rows[0]['instance_id'],r['instance_id'])

    def test_dispatch_and_managed_identity_do_not_repoint_v1(self):
        self.assertIsInstance(forward_runtime(KEY,dataset_id='x',artifact_digest='y'),DonchianV2Runtime)
        v1=registration('donchian_breakout');v2=identity()
        self.assertEqual(v1['definition_id'],v2['definition_id'])
        for k in ('instance_id','version_id','parameter_uuid','parameter_set_id','parameter_hash'):
            self.assertNotEqual(v1[k],v2[k])
        sql=registration_sql()
        self.assertIn("'V2'",sql)
        self.assertNotIn('UPDATE platform.strategy_definition',sql)
        self.assertNotIn('UPDATE strategy_mgmt',sql)
        self.assertNotIn('GRANT ',sql)

    def test_workload_isolated_image_scoped_and_execution_off(self):
        bundle=plan('donchian-v2-test',BASE,IMAGE,now=BASE-1000)
        self.assertEqual(bundle['manifest']['keys'],(KEY,))
        self.assertEqual(bundle['manifest']['symbols'],SYMBOLS)
        doc=bundle['workload']['items'][1]
        self.assertEqual(doc['metadata']['name'],WORKLOAD)
        pod=doc['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['initContainers'][0]['image'],IMAGE)
        self.assertEqual(pod['containers'][0]['image'],IMAGE)
        env={v['name']:v.get('value') for v in pod['containers'][0]['env']}
        self.assertEqual(env['EXECUTION_AUTHORITY_MODE'],'DISABLED')
        sql=bundle['activation_sql']
        self.assertIn('clock_timestamp()',sql)
        self.assertIn('activation_attestation',sql)
        self.assertNotIn('UPDATE platform.strategy_definition',sql)
        self.assertNotIn('execution_eligible=true',sql)
        self.assertIn(identity()['instance_id'],bundle['quiesce_sql'])

    def test_plan_requires_future_t0_and_immutable_image(self):
        for run,at,image in [('bad run',BASE,IMAGE),('valid',BASE-500,IMAGE),('valid',BASE,'image:latest')]:
            with self.assertRaises(ValueError):plan(run,at,image,now=BASE-1000)

    def test_init_verification_uses_only_existing_research_privileges(self):
        config=RunConfig('donchian-v2-test',BASE,(KEY,),dict(SYMBOLS),entry_mode='CONTEMPORANEOUS_CANDLE_OPEN')
        state=ForwardObserver(config).state
        state['activation_attestation']={'schema':'donchian-v2-activation.v1','run_id':config.run_id,
            'image':IMAGE,'instance_id':identity()['instance_id'],'parameter_hash':identity()['parameter_hash'],
            'activation_time':BASE,'execution_eligible':False,'execution_mode':'OFF'}
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.fetchone.return_value=(state,)
        self.assertEqual(verify(conn,config)['status'],'VERIFIED')
        queries=[x.args[0] for x in cur.execute.call_args_list]
        self.assertFalse(any('strategy_mgmt.' in q for q in queries))
        self.assertTrue(any('research.strategy_library_forward_state' in q for q in queries))
        state['activation_attestation']['execution_eligible']=True
        with self.assertRaises(ValueError):verify(conn,config)


if __name__=='__main__':unittest.main()
