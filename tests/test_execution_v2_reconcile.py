"""Minimum broker-reconciliation proof (mission section 10): after an ambiguous submission, ask
the (simulated) bridge's own ledger - never PostgreSQL alone, never a guess - what actually
happened, and record the finding as an additional, immutable fact alongside the original
(unmodified) UNKNOWN_RECONCILIATION_REQUIRED execution_result row.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fakes import FakeConnection
from execution_v2.fence import FenceAuthority
from execution_v2.reconcile import reconcile_attempt

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
KEYS = {"k1": b"0" * 32}
RESOURCE = "execution:demo:ACC1"


def bridge() -> BridgeFenceSimulator:
    return BridgeFenceSimulator(keys=KEYS, configured_account_id="ACC1", clock=lambda: NOW)


class ReconcileAttemptTests(unittest.TestCase):
    def test_no_ledger_entry_reconciles_to_still_unknown(self):
        conn = FakeConnection()
        br = bridge()
        truth = reconcile_attempt(conn, br, "ATT_NEVER_SUBMITTED", now_utc=NOW)
        self.assertEqual(truth, "STILL_UNKNOWN")
        finding = list(conn.tables["execution_v2.reconciliation_finding"].values())[0]
        self.assertEqual(finding["attempt_id"], "ATT_NEVER_SUBMITTED")
        self.assertIsNone(finding["broker_order_id"])

    def test_a_confirmed_filled_dispatch_reconciles_to_confirmed_executed(self):
        conn = FakeConnection()
        br = bridge()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        br.advance_fence(auth.mint_grant(resource=RESOURCE, generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource=RESOURCE, generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
        br.submit(authorization=write_auth, request_fingerprint="fp",
                 broker_call=lambda: {"status": "FILLED", "broker_order_id": "SIMORD-1"})

        truth = reconcile_attempt(conn, br, "ATT_1", now_utc=NOW)

        self.assertEqual(truth, "CONFIRMED_EXECUTED")
        finding = list(conn.tables["execution_v2.reconciliation_finding"].values())[0]
        self.assertEqual(finding["broker_order_id"], "SIMORD-1")

    def test_a_confirmed_rejected_dispatch_reconciles_to_confirmed_not_executed(self):
        conn = FakeConnection()
        br = bridge()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        br.advance_fence(auth.mint_grant(resource=RESOURCE, generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource=RESOURCE, generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
        br.submit(authorization=write_auth, request_fingerprint="fp",
                 broker_call=lambda: {"status": "REJECTED", "reason": "NO_MONEY"})

        truth = reconcile_attempt(conn, br, "ATT_1", now_utc=NOW)
        self.assertEqual(truth, "CONFIRMED_NOT_EXECUTED")

    def test_an_ambiguous_dispatch_reconciles_to_still_unknown_never_auto_resolved(self):
        conn = FakeConnection()
        br = bridge()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        br.advance_fence(auth.mint_grant(resource=RESOURCE, generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource=RESOURCE, generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
        br.submit(authorization=write_auth, request_fingerprint="fp",
                 broker_call=lambda: {"status": "AMBIGUOUS", "reason": "RESPONSE_LOST"})

        truth = reconcile_attempt(conn, br, "ATT_1", now_utc=NOW)
        self.assertEqual(truth, "STILL_UNKNOWN")  # never manufactured into a confirmed fact

    def test_reconciling_twice_records_two_independent_findings_never_rewrites_the_first(self):
        conn = FakeConnection()
        br = bridge()
        auth = FenceAuthority(keys=KEYS, active_key_id="k1")
        br.advance_fence(auth.mint_grant(resource=RESOURCE, generation=1, holder="worker-a", now=NOW))
        write_auth = auth.mint_authorization(resource=RESOURCE, generation=1, attempt_id="ATT_1",
                                             tool="mt5_canonical_order_send", request_fingerprint="fp", now=NOW)
        br.submit(authorization=write_auth, request_fingerprint="fp",
                 broker_call=lambda: {"status": "FILLED", "broker_order_id": "SIMORD-1"})

        first = reconcile_attempt(conn, br, "ATT_1", now_utc=NOW)
        from datetime import timedelta
        second = reconcile_attempt(conn, br, "ATT_1", now_utc=NOW + timedelta(seconds=1))

        self.assertEqual(first, second)
        self.assertEqual(len(conn.tables["execution_v2.reconciliation_finding"]), 2)  # distinct queried_at -> distinct finding_id


if __name__ == "__main__":
    unittest.main()
