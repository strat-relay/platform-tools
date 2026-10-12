"""PostgreSQL tests for the theoretical/broker attribution ownership boundary."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import psycopg

from outcome_resolver import persist_broker_attribution


DSN = os.environ.get("OUTCOME_RESOLVER_TEST_DSN")
if os.environ.get("CI", "").lower() == "true" and not DSN:
    raise RuntimeError("CI must set OUTCOME_RESOLVER_TEST_DSN for broker attribution tests")


@unittest.skipUnless(DSN, "OUTCOME_RESOLVER_TEST_DSN is not set")
class BrokerAttributionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.signal_id = f"TEST_BROKER_ATTR_{uuid4().hex}"
        self.evaluation_id = f"TEST_EVAL_{uuid4().hex}"
        now = datetime.now(timezone.utc)
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'LEGACY_COMPAT', true)")
            cur.execute(
                """INSERT INTO strategy.evaluations
                   (evaluation_id, strategy_id, instrument, direction, decision_time,
                    decision, trace_fidelity, runtime_version, evaluator_version,
                    canonical_payload, canonical_hash)
                   VALUES (%s, 'TEST', 'EURUSD', 'LONG', %s, 'SIGNAL', 'L0',
                           'test', 'test', '{}'::jsonb, %s)""",
                (self.evaluation_id, now, uuid4().hex),
            )
            cur.execute(
                """INSERT INTO strategy.entry_signals
                   (signal_id, candidate_id, evaluation_id, strategy_ref, strategy_id,
                    strategy_version, instrument, direction, decision_time,
                    evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
                   VALUES (%s, %s, %s, 'TEST@V1', 'TEST', 'V1', 'EURUSD', 'LONG',
                           %s, %s, %s, 'ENTRY_SIGNAL_CREATED', %s)""",
                (self.signal_id, self.signal_id, self.evaluation_id, now,
                 uuid4().hex, uuid4().hex, uuid4().hex),
            )
            cur.execute(
                """INSERT INTO strategy.entry_signal_outcomes
                   (signal_id, outcome_type, status, realized_r, exit_timestamp, source,
                    strategy_outcome, strategy_realized_r, strategy_exit_timestamp)
                   VALUES (%s, 'ENTRY_ONLY', 'STOPPED', -1, %s, 'CONTEXT_STRUCTURE_RETRACE_V1',
                           'STOPPED', -1, %s)""",
                (self.signal_id, now, now),
            )
            conn.commit()

    def tearDown(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            # Test-only cleanup bypasses the application trigger. Production
            # writers never receive session_replication_role privileges.
            cur.execute("SET LOCAL session_replication_role = 'replica'")
            cur.execute("DELETE FROM strategy.entry_signal_outcomes WHERE signal_id = %s", (self.signal_id,))
            cur.execute("DELETE FROM strategy.entry_signals WHERE signal_id = %s", (self.signal_id,))
            cur.execute("DELETE FROM strategy.evaluations WHERE evaluation_id = %s", (self.evaluation_id,))
            conn.commit()

    def test_broker_projection_cannot_change_theoretical_fields(self):
        now = datetime.now(timezone.utc)
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            persist_broker_attribution(
                cur, signal_id=self.signal_id, strategy_outcome="TARGET_HIT",
                strategy_realized_r=1, strategy_exit_timestamp=now,
                execution_outcome="STOPPED", broker_realized_r=-1.2,
                broker_fill_timestamp=now, broker_exit_timestamp=now,
                broker_exit_reason="STOP_LOSS", updated_at=now,
            )
            conn.commit()
            cur.execute("""SELECT status, realized_r, exit_timestamp,
                                  strategy_outcome, strategy_realized_r,
                                  execution_outcome, broker_realized_r
                             FROM strategy.entry_signal_outcomes
                            WHERE signal_id = %s""", (self.signal_id,))
            row = cur.fetchone()
        self.assertEqual(row[0], "STOPPED")
        self.assertEqual(float(row[1]), -1.0)
        self.assertEqual(row[3:5], ("STOPPED", -1))
        self.assertEqual(row[5], "STOPPED")
        self.assertAlmostEqual(float(row[6]), -1.2)

    def test_database_guard_rejects_cross_domain_theoretical_update(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'BROKER_ATTRIBUTION', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("""UPDATE strategy.entry_signal_outcomes
                                  SET status = 'TARGET_HIT', realized_r = 1,
                                      strategy_outcome = 'TARGET_HIT', strategy_realized_r = 1
                                WHERE signal_id = %s""", (self.signal_id,))
            conn.rollback()

    def test_database_guard_rejects_cross_domain_broker_update(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'LEGACY_COMPAT', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("""UPDATE strategy.entry_signal_outcomes
                                  SET execution_outcome = 'TARGET_HIT'
                                WHERE signal_id = %s""", (self.signal_id,))
            conn.rollback()

    def test_missing_and_unknown_domains_fail_closed(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', '', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("UPDATE strategy.entry_signal_outcomes SET status = status WHERE signal_id = %s",
                            (self.signal_id,))
            conn.rollback()
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'UNKNOWN', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("UPDATE strategy.entry_signal_outcomes SET status = status WHERE signal_id = %s",
                            (self.signal_id,))
            conn.rollback()

    def test_insert_and_delete_are_not_unowned_mutations(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'BROKER_ATTRIBUTION', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                                  (signal_id, outcome_type, status, source)
                                  VALUES (%s, 'ENTRY_ONLY', 'OPEN', 'CONTEXT_STRUCTURE_RETRACE_V1')""",
                            (f"missing-{self.signal_id}",))
            conn.rollback()
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'LEGACY_COMPAT', true)")
            with self.assertRaises(psycopg.Error):
                cur.execute("DELETE FROM strategy.entry_signal_outcomes WHERE signal_id = %s",
                            (self.signal_id,))
            conn.rollback()

    def test_marker_is_transaction_local_and_does_not_leak_after_commit(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('platform.outcome_writer_domain', 'BROKER_ATTRIBUTION', true)")
            conn.commit()
            cur.execute("SELECT current_setting('platform.outcome_writer_domain', true)")
            self.assertIn(cur.fetchone()[0], (None, ""))


if __name__ == "__main__":
    unittest.main()
