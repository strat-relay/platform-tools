"""Real PostgreSQL rollout checks, restricted to explicitly named test databases."""
import hashlib
import os
from pathlib import Path
import unittest
from research.ict_smc_forward_plan import plan,registration_sql
from research.strategy_library_registration import registration
from strategy_backtest.library.selection import EXPANSION_KEYS

DSN=os.environ.get('OUTCOME_RESOLVER_TEST_DSN')

@unittest.skipUnless(DSN,'isolated PostgreSQL test DSN unavailable')
class IctSmcForwardPostgresTests(unittest.TestCase):
    def test_registration_activation_idempotency_and_versioned_instance_discovery(self):
        import psycopg
        from orchestration.config import load_instances_from_database
        conn=psycopg.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database()');name=cur.fetchone()[0]
                self.assertTrue('test' in name.lower() or name.lower().endswith('_ci'),'refusing writes outside isolated test database')
                cur.execute("SET LOCAL statement_timeout='8s'")
                sql=registration_sql();cur.execute(sql);cur.execute(sql)
                cur.execute("INSERT INTO platform.schema_migrations(version,filename,checksum_sha256) VALUES('066','066_ict_smc_forward_registration.sql',%s) ON CONFLICT DO NOTHING",(hashlib.sha256(sql.encode()).hexdigest(),))
                _,activate=plan('ict-postgres-test',1792346400,'ghcr.io/strat-relay/trading-platform-runtime@sha256:'+'a'*64)
                # Activation is emitted as a standalone transaction. Keep this
                # test inside its rollback-only fixture transaction instead.
                activate=activate.removeprefix('BEGIN; ').removesuffix('COMMIT;\n')
                cur.execute(activate);cur.execute(activate)
            instances=load_instances_from_database(conn)
            for key in EXPANSION_KEYS:
                r=registration(key)
                row=next(i for i in instances if i['instance_id']==r['instance_id'])
                self.assertEqual(row['strategy_id'],r['strategy_id'])
                self.assertEqual(row['library_key'],key)
                self.assertTrue(row['enabled'])
                self.assertEqual(row['execution_mode'],'OFF')
        finally:
            conn.rollback();conn.close()
