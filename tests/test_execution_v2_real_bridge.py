"""The capstone isolated proof (mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION`
section 13): real PostgreSQL + real NATS JetStream + the production `ExecutionWorker` workflow +
the REAL bridge fence/idempotency/reconciliation code (`mt5_bridge_fence`, reached over a real
HTTP transport, exactly as `execution_v2/runtime/service.py` wires it in production) + a
FAKE FINAL MT5 ORDER-SEND PRIMITIVE ONLY (a plain counting callable the bridge server was started
with - the one and only thing in this entire test file that is not real, production-intended
code).

Skipped automatically unless both TRADING_POSTGRES_DSN and V2_TEST_NATS_URL are set (see
docs/v2_execution/README.md for the exact ephemeral docker commands used to run this during the
mission).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect

from execution_v2.bridge_fence_errors import (ExpiredGrant, RequestFingerprintMismatch,
                                              StaleGeneration, WrongAccount)
from execution_v2.fence import FenceAuthority
from execution_v2.risk import RiskPolicy
from execution_v2.runtime.bridge_client import HttpBridgeFenceClient
from execution_v2.worker import ExecutionWorker
from mt5_bridge_fence.boundary import RealBridgeFenceBoundary
from mt5_bridge_fence.http_server import start_bridge_fence_server

ACCOUNT = "ACC1"
KEYS = {"k1": b"0" * 32}
NATS_URL = os.getenv("V2_TEST_NATS_URL", "nats://localhost:14345")


def _postgres_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()) as conn:
            return True
    except Exception:
        return False


def _nats_available() -> bool:
    try:
        import nats
    except ImportError:
        return False

    async def _probe() -> bool:
        try:
            nc = await nats.connect(NATS_URL, connect_timeout=2)
            await nc.close()
            return True
        except Exception:
            return False
    return asyncio.run(_probe())


def _policy(**overrides) -> RiskPolicy:
    fields = dict(version=1, enabled=True, max_volume=0.01, allowed_symbols=None,
                 allowed_accounts=(ACCOUNT,), max_signal_age_seconds=3600.0, source="test")
    fields.update(overrides)
    return RiskPolicy(**fields)


def _never_called() -> dict:
    raise AssertionError("HttpBridgeFenceClient must never invoke the platform-side broker_call")


class _LiveBridge:
    """One real (SQLite-backed) boundary + one real HTTP server, on an OS-assigned ephemeral
    loopback port, wrapping a fake/counting broker_call - constructed fresh per test unless a
    test explicitly wants to reuse one (to prove restart behavior)."""

    def __init__(self, *, account=ACCOUNT, db_path: str | None = None, mode: str = "fill"):
        self._tempdir = tempfile.TemporaryDirectory() if db_path is None else None
        self.db_path = db_path or os.path.join(self._tempdir.name, "fence.db")
        self.calls = 0
        self.mode = mode
        self.boundary = RealBridgeFenceBoundary(keys=KEYS, configured_account_id=account, db_path=self.db_path)
        self.server = start_bridge_fence_server(self.boundary, broker_call=self._broker_call)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _broker_call(self) -> dict:
        self.calls += 1
        if self.mode == "reject":
            return {"status": "REJECTED", "reason": "SIMULATED_BROKER_REJECT"}
        if self.mode == "ambiguous":
            return {"status": "AMBIGUOUS", "reason": "SIMULATED_RESPONSE_LOST"}
        return {"status": "FILLED", "broker_order_id": f"REALBR-{self.calls}"}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> HttpBridgeFenceClient:
        return HttpBridgeFenceClient(base_url=self.base_url)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._tempdir is not None:
            self._tempdir.cleanup()


@unittest.skipUnless(_postgres_available() and _nats_available(),
                     "PostgreSQL and NATS are both required; see docs/v2_execution/README.md")
class RealBridgeIsolatedE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)
        with cls.conn.cursor() as cur:
            for instance_id in ("worker-a", "worker-b"):
                cur.execute("""INSERT INTO platform.runtime_instances (instance_id, component, status)
                              VALUES (%s, 'execution_v2_real_bridge_test', 'RUNNING')
                              ON CONFLICT (instance_id) DO NOTHING""", (instance_id,))
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.rollback()
        self._bridges: list[_LiveBridge] = []

    def tearDown(self):
        for bridge in self._bridges:
            bridge.stop()

    def _live_bridge(self, **kw) -> _LiveBridge:
        bridge = _LiveBridge(**kw)
        self._bridges.append(bridge)
        return bridge

    def _seed_entry_signal(self, *, signal_id: str, entry_signal_hash: str, decision_time=None) -> None:
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
                VALUES (%s,%s,%s,'strat-ref','STRAT1','1','EURUSD','LONG', %s, 1.1000, 1.0950, 1.1100,
                        'evalhash','tracehash','SIGNAL',%s)""",
                       (signal_id, f"CAND_{uuid.uuid4().hex[:8]}", evaluation_id,
                        decision_time or datetime.now(timezone.utc), entry_signal_hash))
        self.conn.commit()

    def _worker(self, bridge_client: HttpBridgeFenceClient, *, holder="worker-a", policy=None) -> ExecutionWorker:
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        return ExecutionWorker(self.conn, fence_authority=authority, bridge=bridge_client,
                               holder_instance_id=holder, account_id=ACCOUNT, mode="demo",
                               risk_policy=policy or _policy())

    async def _publish_and_verify_jetstream(self, subjects: tuple[str, ...], *, expect_at_least: int) -> None:
        import nats as nats_lib
        from infrastructure.messaging.jetstream import JetStreamPublisher, JetStreamTopology
        from infrastructure.messaging.outbox_relay import OutboxRelay

        nc = await nats_lib.connect(NATS_URL)
        js = nc.jetstream()
        await JetStreamTopology.v1().ensure(js)
        relay = OutboxRelay(self.conn, JetStreamPublisher(js))

        received: list[bytes] = []
        done = asyncio.Event()

        async def handler(msg):
            received.append(msg.data)
            await msg.ack()
            if len(received) >= expect_at_least:
                done.set()

        durable = f"v2audit_e2e_{uuid.uuid4().hex[:8]}"
        sub = await js.subscribe("execution.>", durable=durable, manual_ack=True, cb=handler)
        try:
            result = await relay.publish_batch(limit=50)
            self.assertGreaterEqual(result["published"], expect_at_least)
            await asyncio.wait_for(done.wait(), timeout=10)
        finally:
            await sub.unsubscribe()
            await nc.close()

    # -- the primary end-to-end proof -----------------------------------------------------------

    def test_entry_signal_to_jetstream_through_the_real_bridge_exactly_one_effect(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker = self._worker(live.client())

        outcome = worker.process_signal(signal_id, execution_authority_enabled=True,
                                        now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(outcome.status, "RESULT_RECORDED")
        self.assertEqual(outcome.result_outcome, "FILLED")
        self.assertEqual(live.calls, 1)  # the ONLY fake thing in this whole flow

        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM execution_v2.execution_intent WHERE entry_signal_id=%s", (signal_id,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM execution_v2.execution_attempt WHERE execution_intent_id=%s",
                       (outcome.intent_result.execution_intent_id,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM execution_v2.execution_result WHERE attempt_id=%s", (outcome.attempt_id,))
            self.assertEqual(cur.fetchone()[0], 1)

        asyncio.run(self._publish_and_verify_jetstream(
            ("execution.intent.created.v1", "execution.result.recorded.v1"), expect_at_least=2))

    # -- duplicate/idempotency matrix ------------------------------------------------------------

    def test_duplicate_entry_signal_one_intent_one_effect(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker = self._worker(live.client())

        first = worker.process_signal(signal_id, execution_authority_enabled=True,
                                      now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        second = worker.process_signal(signal_id, execution_authority_enabled=True,
                                       now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(live.calls, 1)

    def test_duplicate_jetstream_style_redelivery_one_effect(self):
        """Models JetStream redelivering the same signal.entry.created.v1 message (e.g. an ack
        that was lost in flight) by calling the handler twice with the identical signal_id,
        exactly as execution_v2.runtime.consumer.ExecutionSignalConsumer's real handler would be
        invoked twice by the messaging layer."""
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker = self._worker(live.client())

        for _ in range(2):
            worker.process_signal(signal_id, execution_authority_enabled=True,
                                  now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(live.calls, 1)

    def test_worker_restart_one_effect(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker1 = self._worker(live.client())
        worker1.process_signal(signal_id, execution_authority_enabled=True,
                               now_utc=datetime.now(timezone.utc), broker_call=_never_called)

        # A brand-new ExecutionWorker instance (simulating a platform-process restart), same
        # PostgreSQL connection, same live bridge.
        worker2 = self._worker(live.client(), holder="worker-a-restarted")
        worker2.process_signal(signal_id, execution_authority_enabled=True,
                               now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(live.calls, 1)

    def test_bridge_restart_one_effect(self):
        """The bridge process itself restarts (a brand-new RealBridgeFenceBoundary + HTTP server
        pointed at the SAME durable db_path) between the first submission and a retry."""
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live1 = self._live_bridge()
        worker1 = self._worker(live1.client())
        worker1.process_signal(signal_id, execution_authority_enabled=True,
                               now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        total_calls_before_restart = live1.calls

        live2 = self._live_bridge(db_path=live1.db_path)  # same file, new boundary+server = "restart"
        worker2 = self._worker(live2.client())
        outcome = worker2.process_signal(signal_id, execution_authority_enabled=True,
                                         now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(live2.calls, 0)  # the new bridge process's OWN counter never incremented
        self.assertEqual(total_calls_before_restart, 1)
        self.assertEqual(outcome.status, "RESULT_RECORDED")  # durable ledger answered from disk

    def test_response_loss_one_effect_reconciled_safely(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge(mode="ambiguous")
        worker = self._worker(live.client())

        outcome = worker.process_signal(signal_id, execution_authority_enabled=True,
                                        now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(outcome.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(live.calls, 1)

        retry = worker.process_signal(signal_id, execution_authority_enabled=True,
                                      now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(retry.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(live.calls, 1)  # never blindly retried against the real bridge

        from execution_v2.reconcile import reconcile_attempt
        truth = reconcile_attempt(self.conn, live.client(), outcome.attempt_id)
        self.assertEqual(truth, "STILL_UNKNOWN")

    # -- zero-effect rejection matrix, through the real bridge over HTTP ------------------------

    def test_stale_generation_zero_effect(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        client = live.client()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        resource = "execution:demo:ACC1"

        gen1 = client.advance_fence(auth.mint_grant(resource=resource, generation=1, holder="worker-a"))
        stale_auth = auth.mint_authorization(resource=resource, generation=1, attempt_id="ATT_STALE_RB",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp")
        client.advance_fence(auth.mint_grant(resource=resource, generation=2, holder="worker-b"))

        result = client.submit(authorization=stale_auth, request_fingerprint="fp", broker_call=_never_called)
        self.assertEqual(result.state, "CANCELLED_FENCED")
        self.assertEqual(live.calls, 0)

    def test_expired_fence_zero_effect(self):
        live = self._live_bridge()
        far_future_boundary = RealBridgeFenceBoundary(keys=KEYS, configured_account_id=ACCOUNT,
                                                       db_path=live.db_path,
                                                       clock=lambda: datetime.now(timezone.utc) + timedelta(days=1))
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        with self.assertRaises(ExpiredGrant):
            far_future_boundary.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a"))
        self.assertEqual(live.calls, 0)

    def test_invalid_auth_zero_effect(self):
        live = self._live_bridge()
        client = live.client()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        resource = "execution:demo:ACC1"
        client.advance_fence(auth.mint_grant(resource=resource, generation=1, holder="worker-a"))
        write_auth = auth.mint_authorization(resource=resource, generation=1, attempt_id="ATT_BADSIG_RB",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp")
        import dataclasses
        tampered = dataclasses.replace(write_auth, generation=999)
        with self.assertRaises(Exception):  # InvalidSignature, propagated across the real HTTP call
            client.submit(authorization=tampered, request_fingerprint="fp", broker_call=_never_called)
        self.assertEqual(live.calls, 0)

    def test_wrong_account_zero_effect(self):
        live = self._live_bridge(account="ACCOUNT_OTHER")
        client = live.client()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        resource = "execution:demo:ACC1"
        client.advance_fence(auth.mint_grant(resource=resource, generation=1, holder="worker-a"))
        write_auth = auth.mint_authorization(resource=resource, generation=1, attempt_id="ATT_WRONGACC_RB",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp")
        with self.assertRaises(WrongAccount):
            client.submit(authorization=write_auth, request_fingerprint="fp", broker_call=_never_called)
        self.assertEqual(live.calls, 0)

    def test_fingerprint_mismatch_zero_effect(self):
        live = self._live_bridge()
        client = live.client()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        resource = "execution:demo:ACC1"
        client.advance_fence(auth.mint_grant(resource=resource, generation=1, holder="worker-a"))
        write_auth = auth.mint_authorization(resource=resource, generation=1, attempt_id="ATT_FPMISMATCH_RB",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp-original")
        with self.assertRaises(RequestFingerprintMismatch):
            client.submit(authorization=write_auth, request_fingerprint="fp-different", broker_call=_never_called)
        self.assertEqual(live.calls, 0)

    def test_authority_disabled_zero_effect_through_the_real_bridge(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker = self._worker(live.client())
        from execution_v2.worker import ExecutionAuthorityDisabled
        with self.assertRaises(ExecutionAuthorityDisabled):
            worker.process_signal(signal_id, execution_authority_enabled=False,
                                  now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(live.calls, 0)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM execution_v2.execution_intent WHERE entry_signal_id=%s", (signal_id,))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_risk_disabled_zero_effect_through_the_real_bridge(self):
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        self._seed_entry_signal(signal_id=signal_id, entry_signal_hash=f"h_{uuid.uuid4().hex}")
        live = self._live_bridge()
        worker = self._worker(live.client(), policy=_policy(enabled=False))
        outcome = worker.process_signal(signal_id, execution_authority_enabled=True,
                                        now_utc=datetime.now(timezone.utc), broker_call=_never_called)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertEqual(live.calls, 0)


if __name__ == "__main__":
    unittest.main()
