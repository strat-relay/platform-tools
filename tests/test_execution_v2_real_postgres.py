"""Real-PostgreSQL proof for execution_v2 (mission sections 13/17): the same production code in
`intent.py`/`worker.py`/`reconcile.py` driven against an ephemeral, real PostgreSQL instance
(migrations applied through `python -m postgres.migrate`, matching the P4.2-integration mission's
convention) rather than `fakes.py`. Proves the real `platform.assert_generation()` function, the
real immutability triggers, and the real `ON CONFLICT` targets actually behave as the fakes only
simulate.

Skipped automatically when no PostgreSQL is reachable (set TRADING_POSTGRES_DSN, e.g. via the
ephemeral docker run documented in docs/v2_execution/README.md).
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fence import FenceAuthority
from execution_v2.intent import create_execution_intent
from execution_v2.reconcile import reconcile_attempt
from execution_v2.risk import RiskPolicy
from execution_v2.worker import ExecutionWorker

ACCOUNT = "ACC1"
KEYS = {"k1": b"0" * 32}


def _database_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()) as conn:
            return True
    except Exception:
        return False


def policy(**overrides) -> RiskPolicy:
    fields = dict(version=1, enabled=True, max_volume=0.01, allowed_symbols=None,
                 allowed_accounts=(ACCOUNT,), max_signal_age_seconds=3600.0, source="test")
    fields.update(overrides)
    return RiskPolicy(**fields)


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; see docs/v2_execution/README.md")
class RealPostgresExecutionV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)  # includes 016_execution_v2_foundation.sql; idempotent
        # platform.ownership_leases.holder_instance_id is a real FK to platform.runtime_instances
        # (mirrors production: a worker must register itself before it can acquire a lease) - the
        # fakes.py substrate does not model this FK at all, so this is only discoverable here.
        with cls.conn.cursor() as cur:
            for instance_id in ("worker-a", "worker-b"):
                cur.execute("""INSERT INTO platform.runtime_instances (instance_id, component, status)
                              VALUES (%s, 'execution_v2_test', 'RUNNING') ON CONFLICT (instance_id) DO NOTHING""",
                           (instance_id,))
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.rollback()  # guard against a prior test leaving the shared connection aborted

    def _seed_entry_signal(self, *, signal_id: str, entry_signal_hash: str) -> None:
        evaluation_id = f"EVAL_{uuid.uuid4().hex[:12]}"
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.evaluations
                (evaluation_id, strategy_id, instrument, decision_time, decision, trace_fidelity,
                 runtime_version, evaluator_version, canonical_payload, canonical_hash)
                VALUES (%s,'STRAT1','EURUSD', now(), 'SIGNAL', 'L1', 'test', 'test', '{}'::jsonb, %s)""",
                       (evaluation_id, f"HASH_{uuid.uuid4().hex}"))
            cur.execute("""INSERT INTO strategy.entry_signals
                (signal_id, candidate_id, evaluation_id, strategy_ref, strategy_id, strategy_version,
                 instrument, direction, decision_time, entry_price, stop_price, target_price,
                 evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
                VALUES (%s,%s,%s,'strat-ref','STRAT1','1','EURUSD','LONG', now(), 1.1000, 1.0950, 1.1100,
                        'evalhash','tracehash','SIGNAL',%s)""",
                       (signal_id, f"CAND_{uuid.uuid4().hex[:8]}", evaluation_id, entry_signal_hash))
        self.conn.commit()

    def test_assert_generation_matches_the_real_ownership_lease_cas(self):
        lease_key = f"execution:demo:REALPG_{uuid.uuid4().hex[:8]}"
        with self.conn.cursor() as cur:
            cur.execute("SELECT platform.acquire_ownership(%s,%s,%s)", (lease_key, "worker-a", 0))
            generation = cur.fetchone()[0]
        self.conn.commit()
        self.assertEqual(generation, 1)

        with self.conn.cursor() as cur:
            cur.execute("SELECT platform.assert_generation(%s,%s)", (lease_key, 1))  # must not raise
        self.conn.rollback()

        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("SELECT platform.assert_generation(%s,%s)", (lease_key, 99))
        self.conn.rollback()

    def test_execution_intent_insert_and_immutability_trigger_are_real(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"hash_{uuid.uuid4().hex}")

        result = create_execution_intent(self.conn, signal_id=signal_id, account_id=ACCOUNT,
                                         risk_policy=policy(), now_utc=datetime.now(timezone.utc))
        self.assertEqual(result.status, "CREATED")

        # Identity column mutation must be rejected by the real trigger.
        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE execution_v2.execution_intent SET instrument='GBPUSD' WHERE execution_intent_id=%s",
                           (result.execution_intent_id,))
        self.conn.rollback()

        # Status is explicitly NOT frozen by the trigger.
        with self.conn.cursor() as cur:
            cur.execute("UPDATE execution_v2.execution_intent SET status='COMPLETED' WHERE execution_intent_id=%s",
                       (result.execution_intent_id,))
        self.conn.commit()

        # Re-creating from the same signal is idempotent against the real UNIQUE(entry_signal_id).
        duplicate = create_execution_intent(self.conn, signal_id=signal_id, account_id=ACCOUNT,
                                            risk_policy=policy(), now_utc=datetime.now(timezone.utc))
        self.assertEqual(duplicate.status, "DUPLICATE")
        self.assertEqual(duplicate.execution_intent_id, result.execution_intent_id)

    def test_execution_result_is_truly_immutable_against_real_postgres(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"hash_{uuid.uuid4().hex}")

        clock = {"t": datetime.now(timezone.utc)}
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: clock["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        worker = ExecutionWorker(self.conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                 account_id=ACCOUNT, mode="demo", risk_policy=policy())

        outcome = worker.process_signal(signal_id, execution_authority_enabled=True,
                                        now_utc=datetime.now(timezone.utc),
                                        broker_call=lambda: {"status": "FILLED", "broker_order_id": "REALPG-1"})
        self.assertEqual(outcome.status, "RESULT_RECORDED")
        self.assertEqual(outcome.result_outcome, "FILLED")

        with self.conn.cursor() as cur:
            cur.execute("SELECT outcome FROM execution_v2.execution_result WHERE attempt_id=%s", (outcome.attempt_id,))
            self.assertEqual(cur.fetchone()[0], "FILLED")

        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE execution_v2.execution_result SET outcome='REJECTED' WHERE attempt_id=%s",
                           (outcome.attempt_id,))
        self.conn.rollback()

        with self.assertRaises(Exception):
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM execution_v2.execution_result WHERE attempt_id=%s", (outcome.attempt_id,))
        self.conn.rollback()

    def test_duplicate_signal_through_the_full_worker_produces_no_second_broker_effect_real_db(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"hash_{uuid.uuid4().hex}")

        clock = {"t": datetime.now(timezone.utc)}
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: clock["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        worker = ExecutionWorker(self.conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                 account_id=ACCOUNT, mode="demo", risk_policy=policy())

        calls = {"n": 0}
        def broker_call():
            calls["n"] += 1
            return {"status": "FILLED", "broker_order_id": "REALPG-DUP"}

        first = worker.process_signal(signal_id, execution_authority_enabled=True,
                                      now_utc=datetime.now(timezone.utc), broker_call=broker_call)
        second = worker.process_signal(signal_id, execution_authority_enabled=True,
                                       now_utc=datetime.now(timezone.utc), broker_call=broker_call)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(first.attempt_id, second.attempt_id)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM execution_v2.execution_attempt WHERE execution_intent_id=%s",
                       (first.intent_result.execution_intent_id,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_reconciliation_finding_never_rewrites_the_original_result_real_db(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"hash_{uuid.uuid4().hex}")

        clock = {"t": datetime.now(timezone.utc)}
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: clock["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        worker = ExecutionWorker(self.conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                 account_id=ACCOUNT, mode="demo", risk_policy=policy())

        outcome = worker.process_signal(signal_id, execution_authority_enabled=True,
                                        now_utc=datetime.now(timezone.utc),
                                        broker_call=lambda: {"status": "AMBIGUOUS", "reason": "LOST"})
        self.assertEqual(outcome.status, "RECONCILIATION_REQUIRED")

        truth = reconcile_attempt(self.conn, bridge, outcome.attempt_id)
        self.assertEqual(truth, "STILL_UNKNOWN")

        with self.conn.cursor() as cur:
            cur.execute("SELECT outcome FROM execution_v2.execution_result WHERE attempt_id=%s", (outcome.attempt_id,))
            self.assertEqual(cur.fetchone()[0], "UNKNOWN_RECONCILIATION_REQUIRED")  # never rewritten


if __name__ == "__main__":
    unittest.main()
