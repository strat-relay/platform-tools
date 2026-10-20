"""PostgreSQL-backed authority and fencing regression tests.

These tests require OUTCOME_RESOLVER_TEST_DSN when running in CI. The CI
workflow provisions an isolated PostgreSQL database and migrates it through
053; never point this variable at production.
"""
from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverLeaseLost


DSN = os.environ.get("OUTCOME_RESOLVER_TEST_DSN")
if os.environ.get("CI", "").lower() == "true" and not DSN:
    raise RuntimeError("CI must set OUTCOME_RESOLVER_TEST_DSN to an isolated database migrated through 053")


def _conn():
    return psycopg.connect(DSN)


def _transition(mode: str) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("canonical-entry-outcome-writer",))
        cur.execute("""UPDATE platform.outcome_resolver_control
                           SET mode = %s, generation = generation + 1,
                               decided_by = 'test:resolver-authority'
                         WHERE control_name = 'canonical-entry-outcome-writer'""",
                    (mode,))
        conn.commit()


def _runtime(holder: str) -> OutcomeResolverRuntime:
    return OutcomeResolverRuntime(conn=_conn(), candle_store=None, holder_id=holder)


# The release workflow uses unittest discovery. This class is intentionally
# skipped only for local runs without a DSN; CI fails during module import if
# its isolated database was not provisioned.
@unittest.skipUnless(DSN, "OUTCOME_RESOLVER_TEST_DSN is not set")
class AuthorityRegressionTests(unittest.TestCase):
    def test_library_gap_state_satisfies_both_live_schema_constraints(self):
        from datetime import datetime, timezone
        from orchestration.library_outcomes import resolve_library_signal
        from outcome_resolver_runtime import ResolverSignal
        signal = ResolverSignal('gap', 'TREND_FOLLOWING_EMA_PULLBACK', 'BTCUSD',
                                'SHORT', 100, 101, 98, datetime.now(timezone.utc),
                                'MARKET_PAPER_OBSERVATION', {}, {})
        result = resolve_library_signal(signal, [], 15)
        with _conn() as conn, conn.cursor() as cur:
            # Use the migrated database's actual CHECK expressions, rather
            # than duplicating an allowed-state list in the regression test.
            for index, table in enumerate(('strategy.entry_signal_outcomes',
                                          'platform.outcome_resolver_signal_state')):
                cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                               WHERE conrelid = %s::regclass AND contype = 'c'
                                 AND pg_get_constraintdef(oid) LIKE '%%resolution_state%%'""",
                            (table,))
                constraints = cur.fetchall()
                self.assertTrue(constraints, table)
                for check in constraints:
                    cur.execute(f'CREATE TEMP TABLE gap_state_{index} (resolution_state text {check[0]})')
                    cur.execute(f'INSERT INTO gap_state_{index} VALUES (%s)', (result.resolution_state,))
            conn.rollback()

    def setUp(self):
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM platform.outcome_resolver_lease
                         WHERE lease_name = 'canonical-entry-outcome-resolver'""")
            conn.commit()
        _transition("LEGACY_COMPAT")

    def tearDown(self):
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM platform.outcome_resolver_lease
                         WHERE lease_name = 'canonical-entry-outcome-resolver'""")
            conn.commit()
        _transition("LEGACY_COMPAT")

    def test_legacy_and_stopped_denied(self):
        runtime = _runtime("legacy")
        self.assertFalse(runtime.acquire_lease())
        runtime.conn.close()
        _transition("STOPPED")
        runtime = _runtime("stopped")
        self.assertFalse(runtime.acquire_lease())
        runtime.conn.close()

    def test_primary_single_holder_and_restart(self):
        _transition("RESOLVER_PRIMARY")
        first, second = _runtime("first"), _runtime("second")
        self.assertTrue(first.acquire_lease())
        self.assertFalse(second.acquire_lease())
        restarted = _runtime("first")
        self.assertTrue(restarted.acquire_lease())
        self.assertEqual(first.lease_generation, restarted.lease_generation)
        first.conn.close(); second.conn.close(); restarted.conn.close()

    def test_pod_derived_holder_ids_cannot_share_a_lease(self):
        _transition("RESOLVER_PRIMARY")
        pod_a = _runtime("unified-outcome-resolver-7d9f6c8b6f-a")
        pod_b = _runtime("unified-outcome-resolver-7d9f6c8b6f-b")
        self.assertNotEqual(pod_a.holder_id, pod_b.holder_id)
        self.assertTrue(pod_a.acquire_lease())
        self.assertFalse(pod_b.acquire_lease())
        pod_a.conn.close(); pod_b.conn.close()

    def test_generation_fences_old_holder_and_stopped_renewal(self):
        _transition("RESOLVER_PRIMARY")
        old = _runtime("old")
        self.assertTrue(old.acquire_lease())
        _transition("RESOLVER_PRIMARY")
        with self.assertRaises(ResolverLeaseLost):
            with old.conn.cursor() as cur:
                old._assert_lease(cur)
        self.assertFalse(old.acquire_lease())
        _transition("STOPPED")
        self.assertFalse(old.acquire_lease())
        old.conn.close()

    def test_concurrent_transition_is_serialized(self):
        barrier = Barrier(2)
        runtime = _runtime("racing")
        def acquire():
            barrier.wait(); return runtime.acquire_lease()
        def promote():
            barrier.wait(); _transition("RESOLVER_PRIMARY"); return True
        with ThreadPoolExecutor(max_workers=2) as pool:
            acquire_future = pool.submit(acquire)
            promote_future = pool.submit(promote)
            acquired = acquire_future.result()
            self.assertTrue(promote_future.result())
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT control.mode, control.generation, lease.generation
                             FROM platform.outcome_resolver_control control
                        LEFT JOIN platform.outcome_resolver_lease lease
                               ON lease.lease_name = 'canonical-entry-outcome-resolver'
                            WHERE control.control_name = 'canonical-entry-outcome-writer'""")
            mode, authority_generation, lease_generation = cur.fetchone()
        self.assertEqual(mode, "RESOLVER_PRIMARY")
        self.assertEqual(lease_generation, authority_generation if acquired else None)
        runtime.conn.close()

    def test_open_only_selection(self):
        _transition("RESOLVER_PRIMARY")
        runtime = _runtime("resolver")
        for row in runtime._signals(1000):
            with runtime.conn.cursor() as cur:
                cur.execute("SELECT status FROM strategy.entry_signal_outcomes WHERE signal_id=%s",
                            (row.signal_id,))
                status = cur.fetchone()
            self.assertTrue(status is None or status[0] == "OPEN")
        runtime.conn.rollback(); runtime.conn.close()
