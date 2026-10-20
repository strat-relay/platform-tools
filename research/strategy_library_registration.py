"""Generate review-only managed registration SQL; never connects to a database."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from core.strategies.evaluation import canonical_hash
from strategy_backtest.library.selection import EXPANSION_KEYS
from strategy_backtest.library.forward_selection import resolve_forward_definition as resolve_definition, DONCHIAN_V2_KEY

CANARY = ('donchian_breakout', 'ema_pullback', 'fvg_retracement')
VERSIONED_RESEARCH_KEYS = EXPANSION_KEYS + (DONCHIAN_V2_KEY,)
PUBLICATION_KEYS = CANARY + VERSIONED_RESEARCH_KEYS
SCHEMA_ID = 'STRATEGY_LIBRARY_COMPOSITION_V1'
CREATOR = 'strategy-library-forward-candidate-v1'


def registration(key):
    d = resolve_definition(key)
    fingerprint = canonical_hash(d.manifest())
    def identity(role):
        return str(uuid5(NAMESPACE_URL, 'https://stratrelay.app/library/'+d.strategy_id+'/'+d.version+'/'+role))
    return {'key': key, 'strategy_id': d.strategy_id, 'name': d.name, 'family': d.family,
            'definition_id': str(uuid5(NAMESPACE_URL, 'https://stratrelay.app/library/'+d.strategy_id+'/V1/definition')), 'version_id': identity('version'),
            'parameter_uuid': str(uuid5(NAMESPACE_URL, identity('version')+'/'+fingerprint)),
            'instance_id': identity('forward-canary-instance'),
            'parameter_set_id': f'{d.strategy_id}@{d.version}:experimental:{fingerprint[:16]}',
            'parameter_hash': fingerprint, 'parameters': d.manifest(),
            'catalog_key': 'LIBRARY:'+d.strategy_id,
            'evaluator_key': 'library.'+d.key+'@'+d.version}


def quote(value):
    return "'"+str(value).replace("'", "''")+"'"


def js(value):
    return quote(json.dumps(value, sort_keys=True, allow_nan=False))+'::jsonb'


def candidate_sql(keys=CANARY):
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate strategy key')
    statements = ['-- REVIEW CANDIDATE ONLY. Not a numbered migration; not auto-applied.',
                  '-- All instances are offline and execution-ineligible. Existing rows are never updated.',
                  '-- family_key is a unique legacy catalog key, not the family taxonomy.',
                  'INSERT INTO strategy_mgmt.parameter_schema(schema_id,fields,created_by) VALUES ('+
                  quote(SCHEMA_ID)+','+js({'frozen':True,'contract':'resolved-library-definition.v1'})+','+
                  quote(CREATOR)+') ON CONFLICT (schema_id) DO NOTHING;']
    for key in keys:
        r = registration(key)
        d = resolve_definition(key)
        metadata = {'status':'RESEARCH','execution_eligible':False,'family':r['family'],
                    'strategy_id':r['strategy_id'],'library_key':key,
                    'mode':'OBSERVER_ONLY','broker_writes':False,'signal_publication':False}
        statements += [
            'INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key,description,provenance_notes,created_by) VALUES ('+
            ','.join([quote(r['definition_id']),quote(r['name']),quote(r['catalog_key']),
                      quote('Research-only library composition; no validated performance claim.'),
                      quote(json.dumps(metadata,sort_keys=True)),quote(CREATOR)])+
            ') ON CONFLICT DO NOTHING;',
            'INSERT INTO strategy_mgmt.strategy_version(id,definition_id,version_label,evaluator_key,lifecycle,schema_id,release_notes,frozen_at,frozen_by,created_by) VALUES ('+
            ','.join([quote(r['version_id']),quote(r['definition_id']),quote(d.version),quote(r['evaluator_key']),
                      "'FROZEN'",quote(SCHEMA_ID),quote('Frozen implementation only. Research lifecycle; not execution-qualified.'),
                      'now()',quote(CREATOR),quote(CREATOR)])+') ON CONFLICT DO NOTHING;',
            'INSERT INTO strategy_mgmt.parameter_set(id,parameter_set_id,strategy_version_id,schema_id,values,fingerprint,frozen,frozen_at,frozen_by,provenance,created_by) VALUES ('+
            ','.join([quote(r['parameter_uuid']),quote(r['parameter_set_id']),quote(r['version_id']),quote(SCHEMA_ID),
                      js(r['parameters']),quote(r['parameter_hash']),'true','now()',quote(CREATOR),js(metadata),quote(CREATOR)])+
            ') ON CONFLICT DO NOTHING;',
            'INSERT INTO strategy_mgmt.strategy_instance_v2(id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,execution_mode,instruments,attributes,created_by) VALUES ('+
            ','.join([quote(r['instance_id']),quote(r['version_id']),quote(r['parameter_uuid']),
                      quote(r['name']+' '+d.version+' forward observer'),'false','false',"'OFF'",
                      js(['XAUUSD','BTCUSD','USDJPY']),js(metadata),quote(CREATOR)])+') ON CONFLICT DO NOTHING;',
            # A conflicting deterministic identity must abort, never silently bind
            # a different frozen parameter set or overwrite an existing strategy.
            "DO $library$ BEGIN IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_definition d "
            "JOIN strategy_mgmt.strategy_version v ON v.definition_id=d.id "
            "JOIN strategy_mgmt.parameter_set p ON p.strategy_version_id=v.id "
            "JOIN strategy_mgmt.strategy_instance_v2 i ON i.strategy_version_id=v.id AND i.parameter_set_id=p.id "
            "WHERE d.id="+quote(r['definition_id'])+"::uuid AND d.family_key="+quote(r['catalog_key'])+
            " AND v.id="+quote(r['version_id'])+"::uuid AND v.evaluator_key="+quote(r['evaluator_key'])+
            " AND v.version_label="+quote(d.version)+" AND v.lifecycle='FROZEN' AND p.id="+quote(r['parameter_uuid'])+
            "::uuid AND p.parameter_set_id="+quote(r['parameter_set_id'])+" AND p.fingerprint="+quote(r['parameter_hash'])+
            " AND p.values="+js(r['parameters'])+" AND p.frozen AND i.id="+quote(r['instance_id'])+
            "::uuid AND NOT i.online AND NOT i.execution_eligible AND i.execution_mode='OFF' "
            "AND i.attributes="+js(metadata)+") THEN RAISE EXCEPTION 'Library registration identity/safety conflict: "+key+
            "'; END IF; END $library$;"
        ]
    return '\n\n'.join(statements)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    Path(args.output).write_text(candidate_sql())


if __name__ == '__main__': main()
