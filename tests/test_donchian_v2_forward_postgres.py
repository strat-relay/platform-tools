"""Registration, activation and API mapping in the explicitly isolated CI database."""
import os
import time
import unittest

DSN=os.environ.get('OUTCOME_RESOLVER_TEST_DSN')


@unittest.skipUnless(DSN,'isolated PostgreSQL test DSN unavailable')
class DonchianForwardPostgresTests(unittest.TestCase):
    def test_registration_activation_and_catalog_are_version_scoped(self):
        import psycopg
        from research.donchian_v2_forward import identity, plan, registration_sql
        from research.strategy_library_registration import registration
        from orchestration.config import _load_v2_instances_from_database
        from platform_api.strategy_catalog import _MANAGED_INSTANCE_SQL
        r=identity();old=registration('donchian_breakout')
        conn=psycopg.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database()');database=cur.fetchone()[0]
                self.assertTrue('test' in database.lower() or database.lower().endswith('_ci'),
                                'refusing fixture writes outside isolated test database')
                cur.execute("SET LOCAL statement_timeout='10s'")
                cur.execute('SELECT online,execution_eligible,execution_mode,attributes FROM strategy_mgmt.strategy_instance_v2 WHERE id=%s',(old['instance_id'],))
                old_before=cur.fetchone()
                sql=registration_sql().removeprefix('BEGIN; ').removesuffix(' COMMIT;\n')
                cur.execute(sql);cur.execute(sql)
                # Enable the shared parent only inside this rollback-only test fixture.
                cur.execute('UPDATE platform.strategy_definition SET enabled=true WHERE strategy_id=%s',(r['strategy_id'],))
                bundle=plan('donchian-v2-ci',int(time.time())+3600,
                            'ghcr.io/strat-relay/trading-platform-runtime@sha256:'+'a'*64)
                sql=bundle['activation_sql'].removeprefix('BEGIN; ').removesuffix(' COMMIT;\n')
                cur.execute(sql);cur.execute(sql)
                cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s',('donchian-v2-ci',))
                state=cur.fetchone()[0]
                self.assertEqual(state['activation_attestation']['instance_id'],r['instance_id'])
                self.assertFalse(state['activation_attestation']['execution_eligible'])
                cur.execute(_MANAGED_INSTANCE_SQL)
                names=[c.name for c in cur.description]
                rows=[dict(zip(names,row)) for row in cur.fetchall()]
                catalog=next(x for x in rows if x['instance_id']==r['instance_id'])
                self.assertEqual(catalog['strategy_id'],r['strategy_id'])
                self.assertEqual(catalog['strategy_version'],'V2')
                cur.execute('SELECT online,execution_eligible,execution_mode,attributes FROM strategy_mgmt.strategy_instance_v2 WHERE id=%s',(old['instance_id'],))
                self.assertEqual(cur.fetchone(),old_before)
            rows=_load_v2_instances_from_database(conn)
            live=next(x for x in rows if x['instance_id']==r['instance_id'])
            self.assertEqual(live['library_key'],'donchian_breakout@V2')
            self.assertEqual(live['execution_mode'],'OFF')
        finally:
            conn.rollback();conn.close()
