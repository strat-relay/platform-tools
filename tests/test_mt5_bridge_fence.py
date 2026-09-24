"""Isolated proof for the REAL bridge fence boundary (`mt5_bridge_fence.boundary.
RealBridgeFenceBoundary`) - the same OD-06 proof list `tests/test_execution_v2_fence.py` runs
against the test-only in-memory simulator, run here against the durable SQLite-backed
implementation instead, plus the durability/crash-window proofs the simulator cannot make
(mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION` sections 3, 5, 6, 7). Only the final
MT5 order-send primitive (`broker_call`) is ever a fake/counting callable; every fence,
account-binding, generation, expiry, fingerprint, and idempotency check below is the real
production-intended code.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from execution_v2.bridge_fence_errors import (ExpiredGrant, InvalidSignature,
                                              RequestFingerprintMismatch, StaleGeneration,
                                              WrongAccount)
from execution_v2.fence import FenceAuthority
from mt5_bridge_fence.boundary import RealBridgeFenceBoundary
from mt5_bridge_fence.store import FenceStore

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
KEY = b"0" * 32
KEYS = {"k1": KEY}


def authority() -> FenceAuthority:
    return FenceAuthority(keys=KEYS, active_key_id="k1")


class _TempDb:
    def __enter__(self) -> str:
        self._dir = tempfile.TemporaryDirectory()
        return os.path.join(self._dir.name, "fence.db")

    def __exit__(self, *exc) -> bool:
        self._dir.cleanup()
        return False


def boundary(db_path: str, *, account="ACC1", now=NOW, keys=None) -> RealBridgeFenceBoundary:
    clock = {"t": now}
    return RealBridgeFenceBoundary(keys=keys or KEYS, configured_account_id=account,
                                   db_path=db_path, clock=lambda: clock["t"])


class CurrentOwnerAcceptedTests(unittest.TestCase):
    def test_current_owner_accepted(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
            result = br.advance_fence(grant)
            self.assertTrue(result.accepted)
            self.assertEqual(result.generation, 1)

            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp1", now=NOW)
            submitted = br.submit(authorization=write_auth, request_fingerprint="fp1",
                                  broker_call=lambda: {"status": "FILLED"})
            self.assertEqual(submitted.state, "DISPATCHED")
            self.assertEqual(submitted.broker_response["status"], "FILLED")


class StaleOwnerRejectedTests(unittest.TestCase):
    def test_stale_owner_rejected_at_advance(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=5, holder="worker-b", now=NOW))
            stale_grant = auth.mint_grant(resource="execution:demo:ACC1", generation=3, holder="worker-a", now=NOW)
            with self.assertRaises(StaleGeneration):
                br.advance_fence(stale_grant)

    def test_stale_owner_cannot_submit_after_new_owner_advanced(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            old_grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
            br.advance_fence(old_grant)
            old_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_OLD",
                                               tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=2, holder="worker-b", now=NOW))
            calls = {"n": 0}
            def broker_call():
                calls["n"] += 1
                return {"status": "FILLED"}
            result = br.submit(authorization=old_auth, request_fingerprint="fp", broker_call=broker_call)
            self.assertEqual(result.state, "CANCELLED_FENCED")
            self.assertEqual(calls["n"], 0)  # never reaches the broker

    def test_example_generation_41_then_40_rejected_then_42_valid(self):
        """Direct reproduction of the mission's own worked example (section 5)."""
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=41, holder="worker-a", now=NOW))
            with self.assertRaises(StaleGeneration):
                br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=40, holder="worker-x", now=NOW))
            result_41 = br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=41, holder="worker-a", now=NOW))
            self.assertEqual(result_41.generation, 41)
            result_42 = br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=42, holder="worker-b", now=NOW))
            self.assertEqual(result_42.generation, 42)


class ExpiredFenceRejectedTests(unittest.TestCase):
    def test_expired_grant_rejected_at_advance(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db, now=NOW + timedelta(seconds=25))
            grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", ttl_ms=20_000, now=NOW)
            with self.assertRaises(ExpiredGrant):
                br.advance_fence(grant)

    def test_expired_authorization_rejected_at_submit(self):
        with _TempDb() as db:
            auth = authority()
            clock = {"t": NOW}
            br = RealBridgeFenceBoundary(keys=KEYS, configured_account_id="ACC1", db_path=db, clock=lambda: clock["t"])
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", ttl_s=5.0, now=NOW)
            clock["t"] = NOW + timedelta(seconds=6)
            result = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})
            self.assertEqual(result.state, "EXPIRED_BEFORE_DISPATCH")


class InvalidAuthRejectedTests(unittest.TestCase):
    def test_tampered_grant_signature_rejected(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
            import dataclasses
            tampered = dataclasses.replace(grant, generation=999)
            with self.assertRaises(InvalidSignature):
                br.advance_fence(tampered)

    def test_unknown_key_id_rejected(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db, keys={"different-key": b"1" * 32})
            grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
            with self.assertRaises(InvalidSignature):
                br.advance_fence(grant)

    def test_tampered_authorization_signature_rejected(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            import dataclasses
            tampered = dataclasses.replace(write_auth, generation=2)
            with self.assertRaises(InvalidSignature):
                br.submit(authorization=tampered, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})

    def test_request_fingerprint_mismatch_rejected(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp-original", now=NOW)
            with self.assertRaises(RequestFingerprintMismatch):
                br.submit(authorization=write_auth, request_fingerprint="fp-different", broker_call=lambda: {"status": "FILLED"})


class WrongAccountRejectedTests(unittest.TestCase):
    def test_fence_for_account_a_rejected_by_bridge_configured_for_account_b(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db, account="ACCOUNT_B")
            br.advance_fence(auth.mint_grant(resource="execution:real:ACCOUNT_A", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:real:ACCOUNT_A", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            with self.assertRaises(WrongAccount):
                br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})


class DurableIdempotencyTests(unittest.TestCase):
    def test_duplicate_submit_never_calls_the_broker_twice(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            calls = {"n": 0}
            def broker_call():
                calls["n"] += 1
                return {"status": "FILLED", "broker_order_id": "X"}
            first = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
            second = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(first, second)

    def test_idempotency_survives_a_real_process_restart_new_boundary_instance_same_db_file(self):
        """The strongest durability proof: a BRAND NEW RealBridgeFenceBoundary instance,
        pointed at the same db_path, still refuses to call the broker a second time - proving
        the idempotency ledger lives on disk, not in the first instance's memory."""
        with _TempDb() as db:
            auth = authority()
            br1 = boundary(db)
            br1.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            calls = {"n": 0}
            def broker_call():
                calls["n"] += 1
                return {"status": "FILLED", "broker_order_id": "X"}
            br1.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)

            br2 = boundary(db)  # brand new instance, same file - models a bridge process restart
            retry = br2.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(retry.state, "DISPATCHED")

    def test_restart_leaves_a_cleanly_dispatched_entry_alone(self):
        """A submit() that completed both durable writes (SUBMISSION_IN_PROGRESS then
        DISPATCHED with the broker's response) before any restart is a settled fact - the real
        boundary's two-phase write lets restart() distinguish this from a genuine crash-window
        (proven separately by CrashWindowTests), unlike the in-memory-only test simulator, which
        cannot tell the two apart and must treat every prior success as equally uncertain."""
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
            br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})
            epoch_before = br.bridge_epoch
            swept = br.restart()
            self.assertGreater(br.bridge_epoch, epoch_before)
            self.assertEqual(swept, [])  # nothing was genuinely in-flight
            entry = br.ledger_entry("ATT_1")
            self.assertEqual(entry.state, "DISPATCHED")
            self.assertEqual(entry.broker_response["status"], "FILLED")

    def test_generation_advance_cancels_only_pending_entries_for_that_resource(self):
        """The real boundary scopes its cancellation sweep by resource (an improvement over the
        test-only simulator's documented global-sweep simplification) - an in-flight attempt on
        a DIFFERENT resource must not be cancelled by an unrelated generation advance."""
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC2", generation=1, holder="worker-a", now=NOW))
            # Manually leave an in-flight SUBMISSION_IN_PROGRESS row on ACC2's resource.
            br.store.upsert_ledger_entry(attempt_id="ATT_ACC2", resource="execution:demo:ACC2",
                                         state="SUBMISSION_IN_PROGRESS", broker_response=None, now=NOW.isoformat())
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=2, holder="worker-b", now=NOW))
            entry = br.store.get_ledger_entry("ATT_ACC2")
            self.assertEqual(entry["state"], "SUBMISSION_IN_PROGRESS")  # untouched


class CrashWindowTests(unittest.TestCase):
    """Mission section 7: bridge receives request -> records durable in-progress state -> calls
    MT5 -> MT5 accepts -> process dies before the durable success write. Fault-injected directly
    against the store (the layer where the crash actually matters), matching how
    `BridgeFenceSimulator`'s own restart proof does not literally kill a process either."""

    def test_crash_between_broker_accept_and_durable_success_write_is_never_lost_or_resubmitted(self):
        with _TempDb() as db:
            auth = authority()
            br = boundary(db)
            br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
            write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_CRASH",
                                                 tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)

            # Simulate: submit() got as far as its pre-broker-call durable write (proven for
            # real above by DurableIdempotencyTests), MT5 accepted the order, and the process
            # died before the post-call DISPATCHED write ever happened.
            br.store.upsert_ledger_entry(attempt_id="ATT_CRASH", resource="execution:demo:ACC1",
                                         state="SUBMISSION_IN_PROGRESS", broker_response=None, now=NOW.isoformat())

            # A brand-new boundary instance (the restarted process) sweeps on startup.
            br2 = boundary(db)
            swept = br2.restart()
            self.assertIn("ATT_CRASH", swept)
            self.assertEqual(br2.ledger_entry("ATT_CRASH").state, "UNCERTAIN_AFTER_RESTART")

            # A retry (duplicate delivery after the crash) must NOT call the broker again.
            calls = {"n": 0}
            def broker_call():
                calls["n"] += 1
                return {"status": "FILLED"}
            retry = br2.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
            self.assertEqual(calls["n"], 0)
            self.assertEqual(retry.state, "UNCERTAIN_AFTER_RESTART")  # reconciliation required, never re-sent


if __name__ == "__main__":
    unittest.main()
