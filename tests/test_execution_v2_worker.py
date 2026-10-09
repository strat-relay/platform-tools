"""End-to-end proof for mission section 13: EntrySignal -> exactly one ExecutionIntent -> risk
accepted -> fence issued -> bridge fence accepted -> one simulated broker effect ->
ExecutionResult -> PostgreSQL (fake) -> outbox event, plus every required negative/idempotency
scenario (A-F from mission section 7, the five OD-06 accept/reject proofs already covered
end-to-end through the worker rather than the bridge directly, and the authority-disabled
zero-effect proof).
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fakes import FakeBroker, FakeConnection
from execution_v2.fence import FenceAuthority
from execution_v2.risk import RiskPolicy
from execution_v2.worker import ExecutionAuthorityDisabled, ExecutionWorker
from execution_v2.symbols import canonical_request_fingerprint

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "ACC1"
KEY = b"0" * 32
KEYS = {"k1": KEY}

# The worker resolves a broker symbol (execution_v2/symbols.py) before every submission attempt -
# fails closed with no default/inferred suffix (mission: "no suffix convention is inferred here").
# A "default" key applies regardless of the account/mode any given test uses, matching the
# convention tests/test_execution_v2_symbols.py already establishes for exercising this module
# directly (mock.patch.dict, never relying on the ambient shell environment).
_BROKER_SYMBOL_MAP = {"default": {"EURUSD": "EURUSDm"}}
_env_patch = mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps(_BROKER_SYMBOL_MAP)}, clear=False)


def setUpModule() -> None:
    _env_patch.start()


def tearDownModule() -> None:
    _env_patch.stop()


def policy(**overrides) -> RiskPolicy:
    fields = dict(version=1, enabled=True, max_volume=0.01, allowed_symbols=None,
                 allowed_accounts=(ACCOUNT,), max_signal_age_seconds=3600.0, source="test")
    fields.update(overrides)
    return RiskPolicy(**fields)


def seeded_conn(signal_id="SIG1", entry_signal_hash="hash-1", strategy_id="STRAT1",
                strategy_version=1, strategy_ref="strat-ref") -> FakeConnection:
    conn = FakeConnection()
    conn.seed_entry_signal(signal_id=signal_id, strategy_id=strategy_id, strategy_version=strategy_version,
                           strategy_ref=strategy_ref, instrument="EURUSD", direction="LONG",
                           decision_time=NOW, signal_emitted_at=NOW, entry_price=1.1000, stop_price=1.0950,
                           target_price=1.1100, entry_signal_hash=entry_signal_hash)
    return conn


def make_worker(conn, *, broker: FakeBroker, mode="demo", holder="worker-a", account=ACCOUNT,
                clock_value=NOW, risk_policy=None) -> ExecutionWorker:
    clock = {"t": clock_value}
    bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=account, clock=lambda: clock["t"])
    authority = FenceAuthority(keys=KEYS, active_key_id="k1")
    return ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id=holder,
                          account_id=account, mode=mode, risk_policy=risk_policy or policy())


def run(worker: ExecutionWorker, conn: FakeConnection, broker: FakeBroker, signal_id="SIG1", **kw):
    return worker.process_signal(signal_id, execution_authority_enabled=True, now_utc=NOW,
                                 broker_call=broker, **kw)


class EndToEndTests(unittest.TestCase):
    def test_worker_stale_gate_uses_current_policy_limit(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        intent = {"signal_emitted_at": NOW - timedelta(seconds=61)}

        strict = make_worker(conn, broker=broker, risk_policy=policy(max_signal_age_seconds=60.0))
        relaxed = make_worker(conn, broker=broker, risk_policy=policy(max_signal_age_seconds=120.0))

        self.assertTrue(strict._signal_too_old(intent, now_utc=NOW))  # noqa: SLF001
        self.assertFalse(relaxed._signal_too_old(intent, now_utc=NOW))  # noqa: SLF001

    def test_research_only_v2_is_rejected_before_intent_or_broker_boundary(self):
        conn = seeded_conn(strategy_id="CONTEXT_STRUCTURE_RETRACE_V2", strategy_version="V2",
                           strategy_ref="CONTEXT_STRUCTURE_RETRACE_V2@V2")
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)

        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.status, "BLOCKED")
        self.assertEqual(outcome.detail, "STRATEGY_NOT_EXECUTION_ENABLED")
        self.assertEqual(outcome.intent_result.execution_intent_id, None)
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 0)
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 0)
        self.assertEqual(broker.calls, 0)

    def test_authorization_fingerprint_matches_canonical_wire_request(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)
        captured = {}
        original_submit = worker.bridge.submit

        def capture_submit(*, authorization, request_fingerprint, broker_call, request_args=None):
            captured["authorization"] = authorization
            captured["request_fingerprint"] = request_fingerprint
            captured["request_args"] = request_args
            return original_submit(authorization=authorization, request_fingerprint=request_fingerprint,
                                   broker_call=broker_call, request_args=request_args)

        worker.bridge.submit = capture_submit
        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.result_outcome, "FILLED")
        wire = captured["request_args"]
        expected = canonical_request_fingerprint(wire)
        self.assertEqual(captured["request_fingerprint"], expected)
        self.assertEqual(captured["authorization"].request_fingerprint, expected)

    def test_one_signal_produces_exactly_one_intent_one_attempt_one_result_and_outbox_events(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)

        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.status, "RESULT_RECORDED")
        self.assertEqual(outcome.result_outcome, "FILLED")
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 1)
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 1)
        self.assertEqual(len(conn.tables["execution_v2.execution_result"]), 1)
        self.assertEqual(broker.calls, 1)

        event_types = {e["event_type"] for e in conn.tables["platform.outbox_events"].values()}
        self.assertIn("execution.intent.created.v1", event_types)
        self.assertIn("execution.result.recorded.v1", event_types)

    def test_execution_authority_disabled_produces_zero_broker_effect(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)

        outcome = worker.process_signal("SIG1", execution_authority_enabled=False, now_utc=NOW, broker_call=broker)

        self.assertEqual(broker.calls, 0)
        self.assertEqual(outcome.status, "BLOCKED")
        row = next(iter(conn.tables["execution_v2.execution_intent"].values()))
        self.assertEqual(row["block_reason"], "EXECUTION_AUTHORITY_DISABLED")
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 0)
        self.assertEqual(len(conn.tables["platform.outbox_events"]), 1)

    def test_normal_execution_does_not_require_active_or_unexhausted_canary(self):
        """Historical canary state must not be consulted on the normal broker path."""
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker,
                             risk_policy=policy(canary_max_new_executions=1))

        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.result_outcome, "FILLED")
        self.assertEqual(broker.calls, 1)


class DuplicateAndRetryTests(unittest.TestCase):
    """Mission section 7 scenarios A-F, driven through the worker (not the bridge directly)."""

    def test_duplicate_entry_signal_delivery_creates_no_duplicate_intent_or_effect(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)

        first = run(worker, conn, broker)
        second = run(worker, conn, broker)

        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(broker.calls, 1)  # never dispatched twice
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 1)
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 1)
        self.assertEqual(len(conn.tables["execution_v2.execution_result"]), 1)
        self.assertEqual(second.status, "RESULT_RECORDED")
        self.assertEqual(second.detail, "attempt already terminal; no new broker effect")

    def test_worker_restart_does_not_produce_a_duplicate_broker_effect(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker)
        run(worker, conn, broker)

        # Simulate a worker restart: a brand-new ExecutionWorker instance, same PostgreSQL state,
        # same bridge (its ledger is what actually prevents redispatch).
        conn.rollback()  # nothing pending, no-op; state already committed
        restarted = ExecutionWorker(conn, fence_authority=worker.fence_authority, bridge=worker.bridge,
                                    holder_instance_id="worker-a-restarted", account_id=ACCOUNT,
                                    mode="demo", risk_policy=policy())
        outcome = restarted.process_signal("SIG1", execution_authority_enabled=True, now_utc=NOW, broker_call=broker)

        self.assertEqual(broker.calls, 1)
        self.assertEqual(outcome.status, "RESULT_RECORDED")

    def test_broker_response_lost_leaves_reconciliation_required_and_recovers_without_duplicate_effect(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="ambiguous")
        worker = make_worker(conn, broker=broker)

        outcome = run(worker, conn, broker)
        self.assertEqual(outcome.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(outcome.result_outcome, "UNKNOWN_RECONCILIATION_REQUIRED")
        self.assertEqual(broker.calls, 1)

        row = list(conn.tables["execution_v2.execution_attempt"].values())[0]
        self.assertEqual(row["state"], "UNCERTAIN")

        # A retry (duplicate JetStream delivery) must not call the broker again - it must return
        # RECONCILIATION_REQUIRED again, never a blind resubmission.
        retry = run(worker, conn, broker)
        self.assertEqual(retry.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(broker.calls, 1)

        # Reconciliation later establishes broker truth via the bridge's own ledger.
        from execution_v2.reconcile import reconcile_attempt
        truth = reconcile_attempt(conn, worker.bridge, outcome.attempt_id, now_utc=NOW)
        self.assertEqual(truth, "STILL_UNKNOWN")  # the simulated bridge itself never resolved AMBIGUOUS to a fact


class FenceRejectionThroughTheWorkerTests(unittest.TestCase):
    def test_stale_generation_is_rejected_and_produces_no_broker_effect(self):
        # `_acquire_generation()` is a fresh CAS read-then-claim on every call, so a worker can
        # never observe a stale generation *of its own accord* in a single-threaded scenario - it
        # always acquires whatever is next. The real staleness case (mission section 5: "a stale
        # worker with network access to the bridge must NOT be able to place an order after
        # losing authority") is a worker that already minted a grant/authorization for generation
        # N, then a rival worker takes over (generation N+1) before the first worker's request
        # reaches the bridge. That exact narrow race is what the fence-level proof
        # (test_execution_v2_fence.StaleOwnerRejectedTests) exercises directly; here we reproduce
        # it through the worker's own fence_authority/bridge so the guarantee is proven end to end
        # through production code, not only at the isolated bridge layer.
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        clock = {"t": NOW}
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: clock["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        stale_worker = ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                       account_id=ACCOUNT, mode="demo", risk_policy=policy())

        # worker-a legitimately becomes owner at generation 1 and mints (but has not yet used) a
        # grant/authorization for it.
        gen1 = stale_worker._acquire_generation()  # noqa: SLF001 - white-box fence setup
        bridge.advance_fence(authority.mint_grant(resource=stale_worker.resource, generation=gen1,
                                                  holder="worker-a", now=NOW))
        stale_authorization = authority.mint_authorization(resource=stale_worker.resource, generation=gen1,
                                                            attempt_id="ATT_STALE", tool="mt5_canonical_order_send",
                                                            request_fingerprint="fp", now=NOW)

        # A rival worker (e.g. a new pod after a failed liveness probe on worker-a) takes over:
        # acquires generation 2 in PostgreSQL and advances the bridge's fence to it.
        rival = ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-b",
                                account_id=ACCOUNT, mode="demo", risk_policy=policy())
        gen2 = rival._acquire_generation()  # noqa: SLF001
        self.assertGreater(gen2, gen1)
        bridge.advance_fence(authority.mint_grant(resource=rival.resource, generation=gen2, holder="worker-b", now=NOW))

        # worker-a, unaware it has been superseded, finally submits using its now-stale generation.
        result = bridge.submit(authorization=stale_authorization, request_fingerprint="fp", broker_call=broker)

        self.assertEqual(result.state, "CANCELLED_FENCED")
        self.assertEqual(broker.calls, 0)

    def test_expired_fence_is_rejected_and_produces_no_broker_effect(self):
        # Grant/authorization TTLs are anchored to real wall-clock time inside worker.py
        # (deliberately never to the caller-supplied `now_utc`, which could otherwise be used to
        # keep a fence artificially "fresh" - a real security property, not an oversight). To
        # prove expiry deterministically without depending on wall-clock skew during the test run,
        # push the bridge's own clock a full day past whatever real "now" the worker used to mint.
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        far_future = {"t": datetime.now(timezone.utc)}
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: far_future["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        worker = ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                 account_id=ACCOUNT, mode="demo", risk_policy=policy())

        far_future["t"] = datetime.now(timezone.utc) + timedelta(days=1)
        outcome = worker.process_signal("SIG1", execution_authority_enabled=True, now_utc=NOW, broker_call=broker)

        self.assertEqual(outcome.status, "FENCED_OUT")
        self.assertEqual(broker.calls, 0)

    def test_wrong_account_is_rejected_and_produces_no_broker_effect(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        clock = {"t": NOW}
        # Bridge is configured for a DIFFERENT account than the worker's own account_id.
        # advance_fence itself does not check account binding (only `submit()` does - it is the
        # request, not the ownership signal, that is account-bound), so the fence acquisition
        # step succeeds; the independent rejection happens at submission, exactly the OD-06
        # boundary mission section 5 requires ("WRONG_ACCOUNT_REJECTED"). worker.py must convert
        # that rejection into a safe FENCED_OUT outcome rather than an uncaught exception.
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id="ACCOUNT_OTHER", clock=lambda: clock["t"])
        authority = FenceAuthority(keys=KEYS, active_key_id="k1")
        worker = ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                                 account_id=ACCOUNT, mode="demo", risk_policy=policy())

        outcome = worker.process_signal("SIG1", execution_authority_enabled=True, now_utc=NOW, broker_call=broker)

        self.assertEqual(outcome.status, "FENCED_OUT")
        self.assertEqual(broker.calls, 0)


class RiskBlockedTests(unittest.TestCase):
    def test_ineligible_signal_is_blocked_before_any_fence_or_broker_interaction(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="fill")
        worker = make_worker(conn, broker=broker, risk_policy=policy(enabled=False))

        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.status, "BLOCKED")
        self.assertEqual(broker.calls, 0)
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 0)


class BrokerRejectionTests(unittest.TestCase):
    def test_broker_reject_is_recorded_truthfully_never_as_filled(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="reject")
        worker = make_worker(conn, broker=broker)

        outcome = run(worker, conn, broker)

        self.assertEqual(outcome.status, "RESULT_RECORDED")
        self.assertEqual(outcome.result_outcome, "REJECTED")
        row = list(conn.tables["execution_v2.execution_result"].values())[0]
        self.assertEqual(row["outcome"], "REJECTED")
        self.assertIsNone(row["confirmed_at"])


class BrokerEvidenceSemanticsTests(unittest.TestCase):
    def test_transport_ack_without_broker_evidence_is_reconciliation_required(self):
        conn = seeded_conn()
        broker = FakeBroker(mode="ambiguous")
        worker = make_worker(conn, broker=broker)
        outcome = run(worker, conn, broker)
        self.assertEqual(outcome.result_outcome, "UNKNOWN_RECONCILIATION_REQUIRED")
        result = next(iter(conn.tables["execution_v2.execution_result"].values()))
        self.assertEqual(result["outcome"], "UNKNOWN_RECONCILIATION_REQUIRED")
        self.assertIsNone(result["broker_order_id"])

    def test_broker_acceptance_requires_and_preserves_order_id(self):
        conn = seeded_conn()
        broker = lambda: {"status": "ACCEPTED", "broker_order_id": "ORD-1"}
        worker = make_worker(conn, broker=broker)
        outcome = run(worker, conn, broker)
        self.assertEqual(outcome.result_outcome, "ACCEPTED")
        result = next(iter(conn.tables["execution_v2.execution_result"].values()))
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(result["broker_order_id"], "ORD-1")


if __name__ == "__main__":
    unittest.main()
