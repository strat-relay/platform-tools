import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from outcome_resolver import Candle
from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverSignal


UTC = timezone.utc


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def execute(self, sql, params=()):
        if "outcome_resolver_lease" in sql:
            self.rows = [(1,)] if self.conn.lease_available else []
        elif "last_candle_close" in sql:
            self.rows = [(None,)]
        elif "attempt_count" in sql:
            self.rows = [(0,)]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self): return self.rows


class Conn:
    def __init__(self):
        self.lease_available = True
        self.pending_outcome = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self): return Cursor(self)
    def commit(self): self.commits += 1
    def rollback(self):
        self.rollbacks += 1
        self.pending_outcome = False


class Store:
    def candles(self, signal, timeframe):
        opened = signal.decision_time
        return [Candle(opened, opened + timedelta(minutes=15), 102, 100, 101.5)]


class ResolverRecoveryTests(unittest.TestCase):
    def signal(self):
        return ResolverSignal("S1", "NEW", "EURUSD", "LONG", 100, 99, 101,
                              datetime(2026, 1, 1, tzinfo=UTC), "MARKET", {}, {})

    def test_lease_fences_second_worker(self):
        conn = Conn()
        first = OutcomeResolverRuntime(conn=conn, candle_store=Store(), holder_id="one")
        second = OutcomeResolverRuntime(conn=conn, candle_store=Store(), holder_id="two")
        self.assertTrue(first.acquire_lease())
        conn.lease_available = False
        self.assertFalse(second.acquire_lease())
        self.assertEqual(conn.commits, 1)

    def test_failure_after_outcome_before_cursor_rolls_back_both(self):
        conn = Conn()
        runtime = OutcomeResolverRuntime(conn=conn, candle_store=Store(), holder_id="one")
        runtime.lease_generation = 1

        def persist(*args, **kwargs):
            conn.pending_outcome = True

        def crash(*args, **kwargs):
            raise RuntimeError("simulated crash after outcome persistence")

        with patch("outcome_resolver_runtime.persist_outcome_row", side_effect=persist):
            runtime._persist_state = crash
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                runtime.resolve_one(self.signal())
        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(conn.pending_outcome)
        self.assertEqual(conn.commits, 0)


if __name__ == "__main__":
    unittest.main()
