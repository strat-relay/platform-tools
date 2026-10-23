"""Policy query proof using only the explicitly isolated CI database, rolled back."""
import json
import os
import unittest
import uuid

from execution_v2.eligibility import database_policy_reason


@unittest.skipUnless(os.environ.get('OUTCOME_RESOLVER_TEST_DSN'), 'isolated PostgreSQL unavailable')
class PolicyPostgresTests(unittest.TestCase):
    def test_explicit_alias_policy_query_and_research_restriction(self):
        import psycopg
        conn = psycopg.connect(os.environ['OUTCOME_RESOLVER_TEST_DSN'])
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database()')
                db = cur.fetchone()[0].lower()
                self.assertTrue('test' in db or db.endswith('_ci'), 'isolated test database required')
                sid, alias = 'fixture-' + uuid.uuid4().hex, 'alias-' + uuid.uuid4().hex
                definition, version, params, instance = [str(uuid.uuid4()) for _ in range(4)]
                cur.execute('''INSERT INTO platform.strategy_definition
                    (strategy_id,strategy_version,display_name,enabled,updated_by)
                    VALUES (%s,'test','fixture',true,'test')''', (sid,))
                cur.execute('''INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key)
                    VALUES (%s,'fixture',%s)''', (definition, 'unrelated-family-' + uuid.uuid4().hex))
                cur.execute('''INSERT INTO strategy_mgmt.strategy_version(id,definition_id,version_label,evaluator_key)
                    VALUES (%s,%s,'test','unrelated.evaluator')''', (version, definition))
                cur.execute('''INSERT INTO strategy_mgmt.parameter_schema(schema_id,fields)
                    VALUES (%s,'{}'::jsonb)''', (sid,))
                cur.execute('''INSERT INTO strategy_mgmt.parameter_set
                    (id,parameter_set_id,strategy_version_id,schema_id,values,fingerprint)
                    VALUES (%s,%s,%s,%s,'{}'::jsonb,'fixture')''', (params, sid, version, sid))
                cur.execute('''INSERT INTO strategy_mgmt.strategy_instance_v2
                    (id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,
                     execution_mode,execution_mode_revision,attributes)
                    VALUES (%s,%s,%s,'fixture',true,true,'LIVE',3,%s::jsonb)''',
                            (instance, version, params, json.dumps({'strategy_id': sid, 'instance_id': alias})))
                record = {'strategy_id': sid, 'strategy_instance_id': alias,
                          'source_provenance': {'execution_mode_revision': 3}}
                self.assertIsNone(database_policy_reason(conn, record))
                cur.execute('UPDATE platform.strategy_definition SET attributes=%s::jsonb WHERE strategy_id=%s',
                            (json.dumps({'research_only': True}), sid))
                self.assertEqual(database_policy_reason(conn, record), 'STRATEGY_NOT_EXECUTION_ENABLED')
                record['strategy_instance_id'] = 'missing-' + alias
                self.assertEqual(database_policy_reason(conn, record), 'INSTANCE_EXECUTION_POLICY_MISSING')
        finally:
            conn.rollback()
            conn.close()
