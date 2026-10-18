"""Generate an offline registration migration and an explicit forward-only activation.

No network, database, Kubernetes or secret access. Live T0 and image digest are
operator inputs; no placeholder digest or inferred activation time is accepted.
"""
from datetime import datetime,timezone
import json
import hashlib
from pathlib import Path
import re
from research.strategy_library_registration import registration,quote,js,SCHEMA_ID
from strategy_backtest.library.selection import EXPANSION_KEYS,resolve_definition
from research.strategy_library_forward import RunConfig,VERSION

SYMBOLS={'XAUUSD':'XAUUSDm','BTCUSD':'BTCUSDm','USDJPY':'USDJPYm'}
MIGRATION='066_ict_smc_forward_registration.sql'
WORKLOAD='ict-smc-forward-v1-observer'


def registration_sql():
    statements=['-- Additive ICT/SMC research registrations only. No activation or execution authority.',
      "INSERT INTO strategy_mgmt.parameter_schema(schema_id,fields,created_by) VALUES ("+quote(SCHEMA_ID)+","+js({'frozen':True,'contract':'resolved-library-definition.v1'})+",'migration:066') ON CONFLICT DO NOTHING;"]
    for key in EXPANSION_KEYS:
        r=registration(key);d=resolve_definition(key)
        metadata={'status':'RESEARCH','research_only':True,'execution_eligible':False,'strategy_id':r['strategy_id'],'family':r['family'],'library_key':key,'signal_publication':False,'publication_mode':'ORCHESTRATOR_RESEARCH_ONLY','broker_writes':False}
        statements.extend([
          "INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key,description,provenance_notes,created_by) VALUES ("+','.join([quote(r['definition_id']),quote(r['name']),quote(r['catalog_key']),quote(d.hypothesis),quote(json.dumps(metadata,sort_keys=True)),"'migration:066'"])+") ON CONFLICT DO NOTHING;",
          "INSERT INTO strategy_mgmt.strategy_version(id,definition_id,version_label,evaluator_key,lifecycle,schema_id,release_notes,frozen_at,frozen_by,created_by) VALUES ("+','.join([quote(r['version_id']),quote(r['definition_id']),quote(d.version),quote(r['evaluator_key']),"'FROZEN'",quote(SCHEMA_ID),quote('Research-only forward observation; not validated or execution-qualified.'),'now()',"'migration:066'","'migration:066'"])+") ON CONFLICT DO NOTHING;",
          "INSERT INTO strategy_mgmt.parameter_set(id,parameter_set_id,strategy_version_id,schema_id,values,fingerprint,frozen,frozen_at,frozen_by,provenance,created_by) VALUES ("+','.join([quote(r['parameter_uuid']),quote(r['parameter_set_id']),quote(r['version_id']),quote(SCHEMA_ID),js(r['parameters']),quote(r['parameter_hash']),'true','now()',"'migration:066'",js(metadata),"'migration:066'"])+") ON CONFLICT DO NOTHING;",
          "INSERT INTO strategy_mgmt.strategy_instance_v2(id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,execution_mode,instruments,attributes,created_by) VALUES ("+','.join([quote(r['instance_id']),quote(r['version_id']),quote(r['parameter_uuid']),quote(r['name']+' '+d.version+' forward observer'),'false','false',"'OFF'",js(list(SYMBOLS)),js(metadata),"'migration:066'"])+") ON CONFLICT DO NOTHING;",
          "INSERT INTO platform.strategy_definition(strategy_id,strategy_version,display_name,description,adapter,enabled,routes,default_instance_id,attributes,updated_by) VALUES ("+','.join([quote(r['strategy_id']),quote(d.version),quote(d.name),quote('Research-only; no validated performance or broker fill claim.'),"'StrategyLibraryAdapter'",'false',js({'audit':True,'shadow_execution':False,'distribution_queue':True}),quote(r['instance_id']),js({'research_only':True,'execution_eligible':False,'family':d.family}),"'migration:066'"])+") ON CONFLICT DO NOTHING;",
          "DO $ict$ BEGIN IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id JOIN strategy_mgmt.strategy_definition d ON d.id=v.definition_id WHERE i.id="+quote(r['instance_id'])+"::uuid AND v.id="+quote(r['version_id'])+"::uuid AND v.version_label="+quote(d.version)+" AND v.evaluator_key="+quote(r['evaluator_key'])+" AND v.lifecycle='FROZEN' AND p.id="+quote(r['parameter_uuid'])+"::uuid AND p.parameter_set_id="+quote(r['parameter_set_id'])+" AND p.fingerprint="+quote(r['parameter_hash'])+" AND p.values="+js(r['parameters'])+" AND p.frozen AND d.id="+quote(r['definition_id'])+"::uuid AND d.family_key="+quote(r['catalog_key'])+" AND i.attributes->>'library_key'="+quote(key)+" AND i.attributes->>'status'='RESEARCH' AND NOT i.execution_eligible AND i.execution_mode='OFF') THEN RAISE EXCEPTION 'ICT/SMC managed identity conflict'; END IF; IF NOT EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id="+quote(r['strategy_id'])+" AND strategy_version="+quote(d.version)+" AND adapter='StrategyLibraryAdapter' AND default_instance_id="+quote(r['instance_id'])+") THEN RAISE EXCEPTION 'ICT/SMC parent identity conflict'; END IF; END $ict$;"
        ])
        for symbol in SYMBOLS:
            statements.append("INSERT INTO strategy.instrument_membership(strategy_instance_id,strategy_id,canonical_instrument,state,updated_by) VALUES ("+','.join([quote(r['instance_id']),quote(r['strategy_id']),quote(symbol),"'ACTIVE'","'migration:066'"])+") ON CONFLICT DO NOTHING;")
        statements.append("DO $scope$ BEGIN IF (SELECT count(*) FROM strategy.instrument_membership WHERE strategy_instance_id="+quote(r['instance_id'])+" AND strategy_id="+quote(r['strategy_id'])+" AND state='ACTIVE' AND canonical_instrument=ANY(ARRAY["+','.join(quote(x) for x in SYMBOLS)+"])) <> 3 OR (SELECT count(*) FROM strategy.instrument_membership WHERE strategy_instance_id="+quote(r['instance_id'])+" AND state='ACTIVE') <> 3 THEN RAISE EXCEPTION 'ICT/SMC instrument scope conflict'; END IF; END $scope$;")
    return '\n\n'.join(statements)+'\n'


def plan(run_id,activation_time,image):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,53}',run_id):raise ValueError('Explicit DNS-safe immutable run ID required')
    if not re.fullmatch(r'ghcr\.io/strat-relay/trading-platform-runtime@sha256:[0-9a-f]{64}',image):raise ValueError('Verified immutable runtime digest required')
    config=RunConfig(run_id,activation_time,EXPANSION_KEYS,dict(SYMBOLS),entry_mode='CONTEMPORANEOUS_CANDLE_OPEN')
    manifest={'schema':VERSION,'execution_eligible':False,**__import__('dataclasses').asdict(config)}
    sql=["BEGIN; SET LOCAL statement_timeout='15s'; SELECT pg_advisory_xact_lock(hashtext('ict-smc-forward-activation'));",
         "DO $gate$ BEGIN IF NOT EXISTS (SELECT 1 FROM platform.schema_migrations WHERE version='066' AND filename="+quote(MIGRATION)+" AND checksum_sha256="+quote(hashlib.sha256(registration_sql().encode()).hexdigest())+") THEN RAISE EXCEPTION 'Registration migration 066 is required'; END IF; END $gate$;"]
    for key in EXPANSION_KEYS:
        r=registration(key);d=resolve_definition(key)
        attrs={'orchestrator_run_id':run_id,'publication_activation_time':activation_time,'publication_mode':'ORCHESTRATOR_RESEARCH_ONLY','signal_publication':True,'forward_image':image,'forward_entry_mode':config.entry_mode}
        sql.append("DO $gate$ BEGIN IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id WHERE i.id="+quote(r['instance_id'])+"::uuid AND p.fingerprint="+quote(r['parameter_hash'])+" AND v.evaluator_key="+quote(r['evaluator_key'])+" AND NOT i.execution_eligible AND i.execution_mode='OFF' AND i.attributes->>'library_key'="+quote(key)+" AND (i.attributes->>'orchestrator_run_id' IS NULL OR (i.attributes->>'orchestrator_run_id'="+quote(run_id)+" AND (i.attributes->>'publication_activation_time')::bigint="+str(activation_time)+" AND i.attributes->>'forward_image'="+quote(image)+"))) THEN RAISE EXCEPTION 'ICT/SMC activation identity/run conflict'; END IF; END $gate$;")
        sql.append("UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,attributes=attributes || "+js(attrs)+",updated_at=now() WHERE id="+quote(r['instance_id'])+"::uuid;")
        sql.append("UPDATE platform.strategy_definition SET enabled=true,updated_at=now(),updated_by='ict-smc-forward-activation' WHERE strategy_id="+quote(r['strategy_id'])+" AND adapter='StrategyLibraryAdapter' AND strategy_version="+quote(d.version)+";")
    sql.append('COMMIT;')
    return manifest,'\n'.join(sql)+'\n'


def main():
    import argparse
    p=argparse.ArgumentParser(__doc__);p.add_argument('action',choices=['registration','plan']);p.add_argument('--output',required=True);p.add_argument('--run-id');p.add_argument('--activation-time',type=int);p.add_argument('--image');a=p.parse_args()
    path=Path(a.output)
    if a.action=='registration':path.parent.mkdir(parents=True,exist_ok=True);path.write_text(registration_sql());return
    if not a.run_id or not a.activation_time or not a.image:p.error('plan requires --run-id --activation-time --image')
    manifest,sql=plan(a.run_id,a.activation_time,a.image);path.mkdir(parents=True,exist_ok=True)
    quiesce='BEGIN;\n'+'\n'.join("UPDATE strategy_mgmt.strategy_instance_v2 SET online=false,updated_at=now() WHERE id="+quote(registration(key)['instance_id'])+"::uuid AND attributes->>'orchestrator_run_id'="+quote(a.run_id)+";" for key in EXPANSION_KEYS)+'\nCOMMIT;\n'
    (path/'quiesce.sql').write_text(quiesce)
    (path/'run.json').write_text(json.dumps(manifest,indent=2)+'\n');(path/'activation.sql').write_text(sql)
    from research.ict_smc_forward_workload import documents
    (path/'workload.json').write_text(json.dumps(documents(manifest,a.image),indent=2)+'\n')

if __name__=='__main__':main()
