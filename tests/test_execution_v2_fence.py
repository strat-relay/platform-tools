from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_v2.bridge_fence_sim import (BridgeFenceSimulator, ExpiredGrant, InvalidSignature,
                                           StaleGeneration, WrongAccount)
from execution_v2.fence import FenceAuthority

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
KEY = b"0" * 32
KEYS = {"k1": KEY}


def authority(now=NOW) -> FenceAuthority:
    return FenceAuthority(keys=KEYS, active_key_id="k1")


def bridge(*, account="ACC1", now=NOW, keys=None) -> BridgeFenceSimulator:
    clock = {"t": now}
    return BridgeFenceSimulator(keys=keys or KEYS, configured_account_id=account, clock=lambda: clock["t"])


class FenceAuthorityTests(unittest.TestCase):
    def test_requires_a_sufficiently_long_key(self):
        with self.assertRaises(Exception):
            FenceAuthority(keys={"k1": b"short"}, active_key_id="k1")

    def test_from_env_fails_closed_without_a_key(self):
        import os
        saved = os.environ.pop("V2_FENCE_SIGNING_KEY", None)
        try:
            with self.assertRaises(Exception):
                FenceAuthority.from_env()
        finally:
            if saved is not None:
                os.environ["V2_FENCE_SIGNING_KEY"] = saved


class CurrentOwnerAcceptedTests(unittest.TestCase):
    def test_current_owner_accepted(self):
        auth = authority()
        br = bridge()
        grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
        result = br.advance_fence(grant)
        self.assertTrue(result.accepted)
        self.assertEqual(result.generation, 1)

        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp1", now=NOW)
        submitted = br.submit(authorization=write_auth, request_fingerprint="fp1", broker_call=lambda: {"status": "FILLED"})
        self.assertEqual(submitted.state, "DISPATCHED")
        self.assertEqual(submitted.broker_response["status"], "FILLED")


class StaleOwnerRejectedTests(unittest.TestCase):
    def test_stale_owner_rejected_at_advance(self):
        auth = authority()
        br = bridge()
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=5, holder="worker-b", now=NOW))
        stale_grant = auth.mint_grant(resource="execution:demo:ACC1", generation=3, holder="worker-a", now=NOW)
        with self.assertRaises(StaleGeneration):
            br.advance_fence(stale_grant)

    def test_stale_owner_cannot_submit_after_new_owner_advanced(self):
        auth = authority()
        br = bridge()
        old_grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
        br.advance_fence(old_grant)
        old_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_OLD", tool="mt5_canonical_order_send",
                                           request_fingerprint="fp", now=NOW)
        # New owner takes over at generation 2.
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=2, holder="worker-b", now=NOW))
        result = br.submit(authorization=old_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})
        self.assertEqual(result.state, "CANCELLED_FENCED")  # never reaches the broker


class ExpiredFenceRejectedTests(unittest.TestCase):
    def test_expired_grant_rejected_at_advance(self):
        auth = authority()
        br = bridge(now=NOW + timedelta(seconds=25))
        grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", ttl_ms=20_000, now=NOW)  # expires NOW+20s
        with self.assertRaises(ExpiredGrant):
            br.advance_fence(grant)

    def test_expired_authorization_rejected_at_submit(self):
        auth = authority()
        clock = {"t": NOW}
        br = BridgeFenceSimulator(keys=KEYS, configured_account_id="ACC1", clock=lambda: clock["t"])
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1", tool="mt5_canonical_order_send",
                                             request_fingerprint="fp", ttl_s=5.0, now=NOW)
        clock["t"] = NOW + timedelta(seconds=6)  # past the 5s authorization TTL
        result = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})
        self.assertEqual(result.state, "EXPIRED_BEFORE_DISPATCH")


class InvalidAuthRejectedTests(unittest.TestCase):
    def test_tampered_grant_signature_rejected(self):
        auth = authority()
        br = bridge()
        grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
        import dataclasses
        tampered = dataclasses.replace(grant, generation=999)  # changes the signed payload, sig now invalid
        with self.assertRaises(InvalidSignature):
            br.advance_fence(tampered)

    def test_unknown_key_id_rejected(self):
        auth = authority()
        br = bridge(keys={"different-key": b"1" * 32})
        grant = auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW)
        with self.assertRaises(InvalidSignature):
            br.advance_fence(grant)

    def test_tampered_authorization_signature_rejected(self):
        auth = authority()
        br = bridge()
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1", tool="mt5_canonical_order_send",
                                             request_fingerprint="fp", now=NOW)
        import dataclasses
        tampered = dataclasses.replace(write_auth, generation=2)
        with self.assertRaises(InvalidSignature):
            br.submit(authorization=tampered, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})

    def test_request_fingerprint_mismatch_rejected(self):
        auth = authority()
        br = bridge()
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1", tool="mt5_canonical_order_send",
                                             request_fingerprint="fp-original", now=NOW)
        with self.assertRaises(Exception):
            br.submit(authorization=write_auth, request_fingerprint="fp-different", broker_call=lambda: {"status": "FILLED"})


class WrongAccountRejectedTests(unittest.TestCase):
    def test_fence_for_account_a_rejected_by_bridge_configured_for_account_b(self):
        auth = authority()
        br = bridge(account="ACCOUNT_B")
        br.advance_fence(auth.mint_grant(resource="execution:real:ACCOUNT_A", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:real:ACCOUNT_A", generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
        with self.assertRaises(WrongAccount):
            br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})


class IdempotencyAtTheBridgeTests(unittest.TestCase):
    def test_duplicate_submit_never_calls_the_broker_twice(self):
        auth = authority()
        br = bridge()
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1", tool="mt5_canonical_order_send",
                                             request_fingerprint="fp", now=NOW)
        calls = {"n": 0}
        def broker_call():
            calls["n"] += 1
            return {"status": "FILLED", "broker_order_id": "X"}
        first = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
        second = br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=broker_call)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first, second)

    def test_restart_marks_dispatched_but_unresolved_as_uncertain_never_success(self):
        auth = authority()
        br = bridge()
        br.advance_fence(auth.mint_grant(resource="execution:demo:ACC1", generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource="execution:demo:ACC1", generation=1, attempt_id="ATT_1", tool="mt5_canonical_order_send",
                                             request_fingerprint="fp", now=NOW)
        br.submit(authorization=write_auth, request_fingerprint="fp", broker_call=lambda: {"status": "FILLED"})
        epoch_before = br.bridge_epoch
        br.restart()
        self.assertGreater(br.bridge_epoch, epoch_before)
        entry = br.ledger_entry("ATT_1")
        self.assertEqual(entry.state, "UNCERTAIN_AFTER_RESTART")


if __name__ == "__main__":
    unittest.main()
