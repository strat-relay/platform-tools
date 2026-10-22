"""Prepare a reviewable event-run cutover. Writes files only; never contacts DB or NATS."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import time

from research.library_event_store import SCHEMA, encode
from research.strategy_library_forward import ForwardObserver, RunConfig, VERSION, readiness
from research.strategy_library_registration import PUBLICATION_KEYS, registration, quote, js

SYMBOLS={'BTCUSD':'BTCUSDm','XAUUSD':'XAUUSDm','USDJPY':'USDJPYm'}


def plan(run_id, activation_time, image, keys, *, now=None):
    now=time.time() if now is None else now
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,53}',run_id):raise ValueError('fresh DNS-safe run ID required')
    if type(activation_time) is not int or activation_time < now+600:raise ValueError('T0 must be at least ten minutes ahead')
    if not re.fullmatch(r'ghcr\.io/strat-relay/trading-platform-runtime@sha256:[0-9a-f]{64}',image):
        raise ValueError('verified immutable runtime digest required')
    if not keys or any(key not in PUBLICATION_KEYS for key in keys):raise ValueError('registered publication keys required')
    cfg=RunConfig(run_id,activation_time,tuple(keys),dict(SYMBOLS),entry_mode='CONTEMPORANEOUS_CANDLE_OPEN',
                  entry_quote_max_age_seconds=5,transport='JETSTREAM')
    if not all(r['cache_supported'] for r in readiness(cfg)):raise ValueError('unsupported event timeframe')
    observer=ForwardObserver(cfg)
    state=observer.state
    state['activation_attestation']={'schema':SCHEMA,'run_id':run_id,'binding':observer.binding,
        'activation_time':activation_time,'image':image,'transport':'JETSTREAM','execution_eligible':False}
    statements=["BEGIN; SET LOCAL statement_timeout='20s'; SELECT pg_advisory_xact_lock(hashtext('library-event-cutover'));",
        "DO $time$ BEGIN IF extract(epoch from clock_timestamp()) >= "+str(activation_time)+
        " THEN RAISE EXCEPTION 'T0 elapsed: prepare fresh future event run'; END IF; END $time$;",
        "DO $fresh$ BEGIN IF EXISTS (SELECT 1 FROM research.strategy_library_forward_state WHERE run_id="+quote(run_id)+
        ") THEN RAISE EXCEPTION 'Run already exists: inspect state; never overwrite'; END IF; END $fresh$;"]
    for key in keys:
        r=registration(key)
        statements.append("DO $instance$ BEGIN IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i "
            "JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id "
            "WHERE i.id="+quote(r['instance_id'])+"::uuid AND NOT i.online AND NOT i.execution_eligible AND i.execution_mode='OFF' "
            "AND i.attributes->>'status'='RESEARCH' AND v.version_label="+quote(r['parameters']['version'])+
            " AND v.evaluator_key="+quote(r['evaluator_key'])+" AND p.frozen AND p.fingerprint="+quote(r['parameter_hash'])+
            " AND p.values="+js(r['parameters'])+
            ") THEN RAISE EXCEPTION 'Existing frozen OFF research instance must be quiesced before cutover'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id="+quote(r['strategy_id'])+
            " AND enabled AND adapter='StrategyLibraryAdapter') THEN RAISE EXCEPTION 'Enabled library parent required'; END IF; END $instance$;")
        attrs={'orchestrator_run_id':run_id,'publication_activation_time':activation_time,'publication_transport':'JETSTREAM',
            'publication_mode':'ORCHESTRATOR_RESEARCH_ONLY','signal_publication':True,'research_only':True,
            'forward_image':image,'forward_entry_mode':cfg.entry_mode}
        statements.append("UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,attributes=attributes || "+js(attrs)+
            ",updated_at=now() WHERE id="+quote(r['instance_id'])+"::uuid;")
    statements.append("INSERT INTO research.strategy_library_forward_state(run_id,state) VALUES ("+quote(run_id)+","+js(state)+"); COMMIT;")
    quiesce="BEGIN;\n"+'\n'.join("UPDATE strategy_mgmt.strategy_instance_v2 SET online=false,updated_at=now() WHERE id="+
        quote(registration(k)['instance_id'])+"::uuid AND attributes->>'orchestrator_run_id'="+quote(run_id)+";" for k in keys)+"\nCOMMIT;\n"
    return {'manifest':{'schema':VERSION,'execution_eligible':False,**asdict(cfg)},
            'activation_sql':'\n'.join(statements)+'\n','quiesce_sql':quiesce,
            'readiness':{'status':'PREPARED_NOT_ACTIVATED','image':image,'run_id':run_id,
                'activation_time':activation_time,'binding':observer.binding,'keys':list(keys),
                'execution_eligible':False,'broker_writes':0,'prerequisites':[
                    'migration 070','dedicated credentials/ACLs','provisioned bounded streams',
                    'legacy orchestrator skip guard deployed','old run consumers quiesced',
                    'existing frozen research registrations and memberships','future T0 activation transaction']}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True);p.add_argument('--activation-time',type=int,required=True)
    p.add_argument('--image',required=True);p.add_argument('--key',action='append',required=True)
    p.add_argument('--output',required=True)
    a=p.parse_args();bundle=plan(a.run_id,a.activation_time,a.image,a.key)
    root=Path(a.output);root.mkdir(parents=True,exist_ok=False)
    for name,key in [('run.json','manifest'),('readiness.json','readiness')]:
        (root/name).write_text(json.dumps(bundle[key],indent=2)+'\n')
    (root/'activation.sql').write_text(bundle['activation_sql']);(root/'quiesce.sql').write_text(bundle['quiesce_sql'])
    print(json.dumps(bundle['readiness']))


if __name__=='__main__':main()
