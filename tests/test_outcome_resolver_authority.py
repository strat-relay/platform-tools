"""PostgreSQL-backed authority and fencing regression tests.

These tests are intentionally opt-in. Set OUTCOME_RESOLVER_TEST_DSN to an
isolated PostgreSQL database migrated through 053; never point it at production.
"""
from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverLeaseLost


DSN = os.environ.get("OUTCOME_RESOLVER_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="OUTCOME_RESOLVER_TEST_DSN is not set")


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


@pytest.fixture(autouse=True)
def clean_authority_state():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""DELETE FROM platform.outcome_resolver_lease
                      WHERE lease_name = 'canonical-entry-outcome-resolver'""")
        conn.commit()
    _transition("LEGACY_COMPAT")
    yield
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""DELETE FROM platform.outcome_resolver_lease
                      WHERE lease_name = 'canonical-entry-outcome-resolver'""")
        conn.commit()
    _transition("LEGACY_COMPAT")


def _runtime(holder: str) -> OutcomeResolverRuntime:
    return OutcomeResolverRuntime(conn=_conn(), candle_store=None, holder_id=holder)


def test_legacy_and_stopped_deny_lease_acquisition():
    legacy = _runtime("legacy")
    assert not legacy.acquire_lease()
    legacy.conn.close()

    _transition("STOPPED")
    stopped = _runtime("stopped")
    assert not stopped.acquire_lease()
    stopped.conn.close()


def test_primary_allows_one_holder_and_restart_recovery():
    _transition("RESOLVER_PRIMARY")
    first = _runtime("first")
    second = _runtime("second")
    assert first.acquire_lease()
    assert not second.acquire_lease()

    restarted = _runtime("first")
    assert restarted.acquire_lease()
    assert restarted.lease_generation == first.lease_generation
    first.conn.close(); second.conn.close(); restarted.conn.close()


def test_concurrent_control_transition_and_acquisition_is_serialized():
    barrier = Barrier(2)
    runtime = _runtime("racing-resolver")

    def acquire():
        barrier.wait()
        return runtime.acquire_lease()

    def promote():
        barrier.wait()
        _transition("RESOLVER_PRIMARY")
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        acquired, transitioned = [future.result() for future in
                                  (pool.submit(acquire), pool.submit(promote))]
    assert transitioned is True
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT control.mode, control.generation,
                              lease.generation
                         FROM platform.outcome_resolver_control control
                    LEFT JOIN platform.outcome_resolver_lease lease
                           ON lease.lease_name = 'canonical-entry-outcome-resolver'
                        WHERE control.control_name = 'canonical-entry-outcome-writer'""")
        mode, authority_generation, lease_generation = cur.fetchone()
    assert mode == "RESOLVER_PRIMARY"
    if acquired:
        assert lease_generation == authority_generation
    else:
        assert lease_generation is None
    runtime.conn.close()


def test_generation_change_invalidates_old_holder_and_renewal():
    _transition("RESOLVER_PRIMARY")
    old = _runtime("old")
    assert old.acquire_lease()
    _transition("RESOLVER_PRIMARY")

    with pytest.raises(ResolverLeaseLost):
        with old.conn.cursor() as cur:
            old._assert_lease(cur)
    assert not old.acquire_lease()
    old.conn.close()


def test_stopped_rejects_renewal_and_tick_is_writer_safe():
    _transition("RESOLVER_PRIMARY")
    runtime = _runtime("resolver")
    assert runtime.acquire_lease()
    _transition("STOPPED")
    assert not runtime.acquire_lease()
    assert runtime.tick(limit=1)["status"] == "WRITER_NOT_PRIMARY"
    runtime.conn.close()


def test_terminal_rows_are_not_selected_for_open_only_reconciliation():
    _transition("RESOLVER_PRIMARY")
    runtime = _runtime("resolver")
    rows = runtime._signals(1000)
    with runtime.conn.cursor() as cur:
        for row in rows:
            cur.execute("SELECT status FROM strategy.entry_signal_outcomes WHERE signal_id = %s",
                        (row.signal_id,))
            status = cur.fetchone()
            assert status is None or status[0] == "OPEN"
    runtime.conn.rollback()
    runtime.conn.close()


# The release workflow uses unittest discovery. Keep an equivalent discovered
# class in addition to the opt-in pytest functions above so CI exercises the
# authority contract when an isolated DSN is supplied.
@unittest.skipUnless(DSN, "OUTCOME_RESOLVER_TEST_DSN is not set")
class AuthorityRegressionTests(unittest.TestCase):
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
