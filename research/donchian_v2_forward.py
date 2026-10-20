"""Prepare and verify a separate Donchian V2 forward observer. No implicit activation."""
from dataclasses import asdict
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re
import time

from core.strategies.evaluation import canonical_hash
from postgres.config import PostgresConfig
from research.strategy_library_forward import ForwardObserver, RunConfig, VERSION
from research.strategy_library_registration import candidate_sql, registration, quote, js
from strategy_backtest.library.forward_selection import DONCHIAN_V2_KEY as KEY
from strategy_backtest.library.donchian_v2 import DEFINITION

WORKLOAD='donchian-breakout-v2-observer'
SYMBOLS={'BTCUSD':'BTCUSDm','XAUUSD':'XAUUSDm','USDJPY':'USDJPYm'}


def identity():return registration(KEY)


def registration_sql():
    r=identity()
    statements=["BEGIN; SET LOCAL statement_timeout='20s'; SELECT pg_advisory_xact_lock(hashtext('donchian-v2-forward-provision'));",
                candidate_sql((KEY,))]
    # Existing V1 platform parent is shared by strategy ID. Never repoint its version or instance.
    statements.append("DO $parent$ BEGIN IF NOT EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id="+
        quote(r['strategy_id'])+" AND adapter='StrategyLibraryAdapter') THEN RAISE EXCEPTION 'Existing Donchian library parent required'; END IF; END $parent$;")
    for symbol in SYMBOLS:
        statements.append("INSERT INTO strategy.instrument_membership(strategy_instance_id,strategy_id,canonical_instrument,state,updated_by) VALUES ("+
            ','.join([quote(r['instance_id']),quote(r['strategy_id']),quote(symbol),"'ACTIVE'","'donchian-v2-forward'"])+") ON CONFLICT DO NOTHING;")
    statements.append("DO $scope$ BEGIN IF (SELECT count(*) FROM strategy.instrument_membership WHERE strategy_instance_id="+
        quote(r['instance_id'])+" AND state='ACTIVE' AND canonical_instrument=ANY(ARRAY["+','.join(quote(x) for x in SYMBOLS)+
        "]))<>3 OR (SELECT count(*) FROM strategy.instrument_membership WHERE strategy_instance_id="+
        quote(r['instance_id'])+" AND state='ACTIVE')<>3 THEN RAISE EXCEPTION 'V2 instrument scope conflict'; END IF; END $scope$; COMMIT;")
    return '\n'.join(statements)+'\n'


def plan(run_id,activation_time,image,*,now=None):
    now=time.time() if now is None else now
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,53}',run_id):raise ValueError('DNS-safe immutable run ID required')
    if type(activation_time) is not int or activation_time < now+600:
        raise ValueError('Freeze T0 at least ten minutes in the future')
    if not re.fullmatch(r'ghcr\.io/strat-relay/trading-platform-runtime@sha256:[0-9a-f]{64}',image):
        raise ValueError('Verified immutable runtime image required')
    cfg=RunConfig(run_id,activation_time,(KEY,),dict(SYMBOLS),entry_mode='CONTEMPORANEOUS_CANDLE_OPEN')
    manifest={'schema':VERSION,'execution_eligible':False,**asdict(cfg)}
    r=identity()
    attrs={'orchestrator_run_id':run_id,'publication_activation_time':activation_time,
           'publication_mode':'ORCHESTRATOR_RESEARCH_ONLY','signal_publication':True,
           'forward_image':image,'forward_entry_mode':cfg.entry_mode,'research_only':True}
    initial=ForwardObserver(cfg).state
    attestation={'schema':'donchian-v2-activation.v1','run_id':run_id,'image':image,
                 'instance_id':r['instance_id'],'parameter_hash':r['parameter_hash'],
                 'activation_time':activation_time,'execution_eligible':False,'execution_mode':'OFF'}
    initial['activation_attestation']=attestation
    sql=["BEGIN; SET LOCAL statement_timeout='20s'; SELECT pg_advisory_xact_lock(hashtext('donchian-v2-forward-provision'));",
         "DO $time$ BEGIN IF extract(epoch from clock_timestamp()) >= "+str(activation_time)+
         " THEN RAISE EXCEPTION 'T0 elapsed: prepare a fresh run boundary before activation'; END IF; END $time$;",
         "DO $identity$ BEGIN IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id WHERE i.id="+
         quote(r['instance_id'])+"::uuid AND v.id="+quote(r['version_id'])+"::uuid AND v.version_label='V2' AND v.evaluator_key="+
         quote(r['evaluator_key'])+" AND p.id="+quote(r['parameter_uuid'])+"::uuid AND p.fingerprint="+quote(r['parameter_hash'])+
         " AND p.values="+js(r['parameters'])+" AND p.frozen AND NOT i.execution_eligible AND i.execution_mode='OFF' AND i.attributes->>'library_key'="+
         quote(KEY)+" AND i.attributes->>'status'='RESEARCH' AND (i.attributes->>'orchestrator_run_id' IS NULL OR (i.attributes->>'orchestrator_run_id'="+
         quote(run_id)+" AND (i.attributes->>'publication_activation_time')::bigint="+str(activation_time)+
         " AND i.attributes->>'forward_image'="+quote(image)+"))) THEN RAISE EXCEPTION 'V2 identity or frozen run conflict'; END IF; "
         "IF NOT EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id="+quote(r['strategy_id'])+
         " AND adapter='StrategyLibraryAdapter' AND enabled) THEN RAISE EXCEPTION 'Existing library parent must already be enabled'; END IF; END $identity$;",
         "UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,attributes=attributes || "+js(attrs)+
         ",updated_at=now() WHERE id="+quote(r['instance_id'])+"::uuid;",
         "INSERT INTO research.strategy_library_forward_state(run_id,state) VALUES ("+quote(run_id)+","+js(initial)+") ON CONFLICT(run_id) DO NOTHING;",
         "DO $state$ BEGIN IF NOT EXISTS (SELECT 1 FROM research.strategy_library_forward_state WHERE run_id="+quote(run_id)+
         " AND state->>'binding'="+quote(initial['binding'])+" AND state->'activation_attestation'="+js(attestation)+
         ") THEN RAISE EXCEPTION 'V2 research run/checkpoint identity conflict'; END IF; END $state$; COMMIT;"]
    quiesce=("BEGIN; UPDATE strategy_mgmt.strategy_instance_v2 SET online=false,updated_at=now() WHERE id="+quote(r['instance_id'])+
        "::uuid AND attributes->>'orchestrator_run_id'="+quote(run_id)+"; COMMIT;\n")
    from research.ict_smc_forward_workload import documents
    workload=documents(manifest,image,workload=WORKLOAD)
    deployment=workload['items'][1]
    container=deployment['spec']['template']['spec']['containers'][0]
    # The same limited research Secret is used; never provision or copy credentials here.
    verification={k:container[k] for k in ('image','envFrom','env','securityContext','volumeMounts')}
    verification.update(name='verify-research-registration',command=['python','-m','research.donchian_v2_forward','verify','--manifest','/config/run.json'])
    deployment['spec']['template']['spec']['initContainers']=[verification]
    return {'manifest':manifest,'registration_sql':registration_sql(),'activation_sql':'\n'.join(sql)+'\n',
            'quiesce_sql':quiesce,'workload':workload,'identity':r,
            'orchestrator_patch':{'apiVersion':'apps/v1','kind':'Deployment',
                'metadata':{'name':'mt5-native-bridge-main-runtime','namespace':'trading'},
                'spec':{'template':{'spec':{'containers':[{'name':'orchestrator-shadow','image':image}]}}}}}


def verify(conn,config):
    if config.keys!=(KEY,) or config.symbols!=SYMBOLS or config.entry_mode!='CONTEMPORANEOUS_CANDLE_OPEN':
        raise ValueError('V2 isolated manifest scope mismatch')
    r=identity()
    with conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s',(config.run_id,))
        row=cur.fetchone()
    conn.rollback()
    if not row:raise ValueError('V2 activation attestation missing')
    state=row[0];attestation=state.get('activation_attestation',{})
    image=attestation.get('image','')
    if not (state.get('binding')==ForwardObserver(config).binding
            and attestation.get('schema')=='donchian-v2-activation.v1'
            and attestation.get('run_id')==config.run_id
            and attestation.get('instance_id')==r['instance_id']
            and attestation.get('parameter_hash')==r['parameter_hash']
            and attestation.get('activation_time')==config.activation_time
            and attestation.get('execution_eligible') is False and attestation.get('execution_mode')=='OFF'
            and re.fullmatch(r'ghcr\.io/strat-relay/trading-platform-runtime@sha256:[0-9a-f]{64}',image)):
        raise ValueError('V2 research identity/activation verification failed')
    return {'status':'VERIFIED','strategy_ref':r['strategy_id']+'@V2','instance_id':r['instance_id'],
            'run_id':config.run_id,'execution_eligible':False,'broker_writes':0}


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('action',choices=['plan','verify']);p.add_argument('--manifest');p.add_argument('--output')
    p.add_argument('--run-id');p.add_argument('--activation-time',type=int);p.add_argument('--image')
    a=p.parse_args()
    if a.action=='verify':
        if not a.manifest:p.error('--manifest required')
        from postgres.db import connect
        config=RunConfig.load(a.manifest);pg=PostgresConfig.from_env();pg.require_explicit_target()
        with connect(pg) as conn:print(json.dumps(verify(conn,config)))
        return
    if not all((a.run_id,a.activation_time,a.image,a.output)):p.error('plan requires --run-id --activation-time --image --output')
    bundle=plan(a.run_id,a.activation_time,a.image);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    for key,name in [('manifest','run.json'),('workload','workload.json'),('identity','identity.json'),('orchestrator_patch','orchestrator-image.patch.json')]:
        (out/name).write_text(json.dumps(bundle[key],indent=2)+'\n')
    for key,name in [('registration_sql','registration.sql'),('activation_sql','activation.sql'),('quiesce_sql','quiesce.sql')]:
        (out/name).write_text(bundle[key])
    print(json.dumps({'status':'PREPARED_NOT_ACTIVATED','output':str(out),'instance_id':bundle['identity']['instance_id'],'broker_writes':0}))


if __name__=='__main__':main()
