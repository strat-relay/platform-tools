import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from outcome_resolver_runtime import (OutcomeResolverRuntime,
                                      ResolverRedisCandleStore, ResolverSignal)


class FakeRedis:
    def __init__(self, value):
        self.value = value

    def get(self, key):
        self.key = key
        return self.value


class RecordingCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class RecordingConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class OutcomeResolverRuntimeTests(unittest.TestCase):
    def signal(self):
        return ResolverSignal(
            signal_id="SIG-1", strategy_id="NEW_STRATEGY_V1", instrument="EURUSD",
            direction="LONG", entry=1.1, stop=1.0, target=1.2,
            decision_time=datetime(2026, 1, 1, tzinfo=timezone.utc), entry_type="MARKET",
            strategy_metadata={}, source_provenance={"provider_symbol": "EURUSDm"},
        )

    def test_candle_store_is_strategy_agnostic_and_preserves_close(self):
        client = FakeRedis(json.dumps([{"time": 1767225600, "high": 1.2,
                                        "low": 1.05, "close": 1.15}]))
        candles = ResolverRedisCandleStore(client).candles(self.signal(), 15)
        self.assertEqual(client.key, "md:bars:EURUSDm:M15")
        self.assertEqual(candles[0].close, 1.15)

    def test_migration_declares_durable_cursor_lease_and_cutover_fence(self):
        sql = (Path(__file__).resolve().parents[1] /
               "postgres/migrations/052_outcome_resolver_runtime_state.sql").read_text()
        for marker in ("outcome_resolver_signal_state", "outcome_resolver_lease",
                       "outcome_resolver_control", "LEGACY_COMPAT", "RESOLVER_PRIMARY"):
            self.assertIn(marker, sql)

    def test_service_is_fail_closed_without_explicit_primary_flags(self):
        from scripts.outcome_resolver_service import run_once
        with patch.dict(os.environ, {"OUTCOME_RESOLVER_ENABLED": "false",
                                     "OUTCOME_RESOLVER_PRIMARY": "false"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "primary ownership"):
                run_once(limit=1)

    def test_resolver_owns_initial_open_row_creation(self):
        conn = RecordingConnection()
        runtime = OutcomeResolverRuntime(
            conn=conn, candle_store=ResolverRedisCandleStore(FakeRedis(None)),
            holder_id="test-resolver")
        runtime.lease_generation = 1
        runtime._assert_lease = lambda _cur: None
        runtime._cursor = lambda _signal_id: None
        runtime._next_attempt = lambda _signal_id: 1
        runtime._persist_state = lambda *args, **kwargs: None
        calls = []

        def record_persist(_cur, **kwargs):
            calls.append(kwargs)
            return True

        with patch("outcome_resolver_runtime.persist_outcome_row", side_effect=record_persist):
            result = runtime.resolve_one(self.signal())

        self.assertEqual(result.status, "OPEN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "OPEN")
        self.assertEqual(calls[0]["writer_id"], "unified-outcome-resolver")
        self.assertEqual(calls[0]["source_kind"], "STRATEGY_REPLAY")
        self.assertEqual(conn.commits, 1)


if __name__ == "__main__":
    unittest.main()
