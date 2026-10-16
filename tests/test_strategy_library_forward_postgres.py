"""Ledger transaction checks against the explicitly isolated CI database only."""
import os
from pathlib import Path
import unittest
import uuid

from research.strategy_library_forward import PostgresResearchLedger

DSN = os.environ.get('OUTCOME_RESOLVER_TEST_DSN')


@unittest.skipUnless(DSN, 'isolated PostgreSQL test DSN unavailable')
class ForwardLedgerPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.conn = psycopg.connect(DSN)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.conn.rollback)
        with self.conn.cursor() as cur:
            cur.execute('SELECT current_database()')
            name = cur.fetchone()[0]
            if 'test' not in name.lower() and not name.lower().endswith('_ci'):
                self.fail('refusing ledger fixture DDL outside an explicitly named test/CI database')
            cur.execute("SET LOCAL statement_timeout = '5s'")
            sql = Path(__file__).resolve().parents[1] / 'docs/strategy_library/forward/schema_candidate.sql'
            cur.execute(sql.read_text())
        self.run_id = 'test-library-' + uuid.uuid4().hex
        self.ledger = PostgresResearchLedger(self.conn, self.run_id)
        self.record = {'event_id':'event','kind':'EVALUATION','strategy_key':'donchian_breakout',
                       'instrument':'EURUSD','observed_at':1792166400,'execution_eligible':False}

    def test_checkpoint_and_deduplicated_events_share_transaction(self):
        self.assertIsNone(self.ledger.lock_and_load())
        self.ledger.save({'binding':'frozen'}, [self.record])
        self.ledger.save({'binding':'frozen'}, [self.record])
        with self.conn.cursor() as cur:
            cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cur.execute('SELECT count(*) FROM research.strategy_library_forward_event WHERE run_id=%s',
                        (self.run_id,))
            self.assertEqual(cur.fetchone()[0], 1)
        self.assertEqual(self.ledger.lock_and_load(), {'binding':'frozen'})

    def test_failure_after_event_insert_rolls_back_checkpoint_and_event(self):
        self.ledger.lock_and_load()
        self.ledger.save({'binding':'frozen'}, [self.record])
        self.conn.rollback()  # same rollback a failed runtime cycle performs
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('research.strategy_library_forward_event')")
            if cur.fetchone()[0] is not None:
                cur.execute('SELECT count(*) FROM research.strategy_library_forward_event WHERE run_id=%s',
                            (self.run_id,))
                self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT to_regclass('research.strategy_library_forward_state')")
            if cur.fetchone()[0] is not None:
                cur.execute('SELECT count(*) FROM research.strategy_library_forward_state WHERE run_id=%s',
                            (self.run_id,))
                self.assertEqual(cur.fetchone()[0], 0)

    def test_schema_rejects_executable_record(self):
        self.ledger.lock_and_load()
        self.ledger.save({'binding':'frozen'}, [])
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.ledger.save({'binding':'frozen'}, [{**self.record, 'execution_eligible':True}])

    def test_same_run_advisory_lock_excludes_second_connection(self):
        import psycopg
        self.ledger.lock_and_load()
        with psycopg.connect(DSN) as second:
            with second.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '200ms'")
            with self.assertRaises(psycopg.errors.QueryCanceled):
                PostgresResearchLedger(second, self.run_id).lock_and_load()
            second.rollback()

    def test_canary_registration_is_idempotent_and_stays_offline(self):
        from research.strategy_library_registration import CANARY, candidate_sql, registration
        with self.conn.cursor() as cur:
            keys = ('ema_momentum',)  # canary instances are enabled for publication by 064
            cur.execute(candidate_sql(keys))
            cur.execute(candidate_sql(keys))
            for key in keys:
                row = registration(key)
                cur.execute('''SELECT online,execution_eligible,execution_mode
                               FROM strategy_mgmt.strategy_instance_v2 WHERE id=%s''',
                            (row['instance_id'],))
                self.assertEqual(cur.fetchone(), (False, False, 'OFF'))

    def test_registration_conflicting_identity_fails_closed(self):
        import psycopg
        from research.strategy_library_registration import CANARY, candidate_sql, registration
        key = 'ema_momentum'  # not registered by canary migration 063
        row = registration(key)
        with self.conn.cursor() as cur:
            cur.execute('''INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key)
                           VALUES (%s,'Conflicting definition',%s)''',
                        (row['definition_id'], 'conflict-'+self.run_id))
            with self.assertRaises(psycopg.errors.RaiseException):
                cur.execute(candidate_sql((key,)))

    def test_observer_group_has_research_writes_without_delete_or_canonical_writes(self):
        sql = Path(__file__).resolve().parents[1] / 'docs/strategy_library/forward/observer_role_candidate.sql'
        with self.conn.cursor() as cur:
            cur.execute(sql.read_text())
            cur.execute("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname='strategy_library_observer'")
            self.assertEqual(cur.fetchone(), (False,False,False,False,False,False))
            cur.execute("SELECT has_table_privilege('strategy_library_observer','research.strategy_library_forward_event','INSERT'),has_table_privilege('strategy_library_observer','research.strategy_library_forward_event','DELETE'),has_table_privilege('strategy_library_observer','strategy.entry_signals','INSERT')")
            self.assertEqual(cur.fetchone(), (True,False,False))
