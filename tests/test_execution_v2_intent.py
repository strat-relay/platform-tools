"""EntrySignal -> ExecutionIntent proof (mission section 3): pure eligibility checks plus
idempotent, deterministic intent creation against `execution_v2.fakes.FakeConnection` (the same
SQL-surface fake used by the worker/fence test suites - no live PostgreSQL required here; the
real schema is proven separately against real PostgreSQL, see docs/v2_execution/README.md).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_v2.fakes import FakeConnection
from execution_v2.intent import (EntrySignalRecordMissing, check_eligibility,
                                 create_execution_intent)
from execution_v2.risk import RiskPolicy

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "ACC1"


def policy(**overrides) -> RiskPolicy:
    fields = dict(version=1, enabled=True, max_volume=0.01, allowed_symbols=None,
                 allowed_accounts=(ACCOUNT,), max_signal_age_seconds=3600.0, source="test")
    fields.update(overrides)
    return RiskPolicy(**fields)


def valid_record(**overrides) -> dict:
    record = {"instrument": "EURUSD", "direction": "LONG", "entry_price": 1.1000,
             "stop_price": 1.0950, "target_price": 1.1100, "decision_time": NOW,
             "signal_emitted_at": NOW}
    record.update(overrides)
    return record


class CheckEligibilityTests(unittest.TestCase):
    def test_eligible_on_a_valid_record(self):
        result = check_eligibility(valid_record(), risk_policy=policy(), account_id=ACCOUNT, now_utc=NOW)
        self.assertTrue(result.eligible)
        self.assertIsNone(result.reason)

    def test_blocked_when_risk_policy_disabled(self):
        result = check_eligibility(valid_record(), risk_policy=policy(enabled=False), account_id=ACCOUNT, now_utc=NOW)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "RISK_POLICY_DISABLED")

    def test_blocked_when_account_not_allowed(self):
        result = check_eligibility(valid_record(), risk_policy=policy(allowed_accounts=("OTHER",)),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "ACCOUNT_NOT_ALLOWED")

    def test_blocked_when_symbol_not_allowed(self):
        result = check_eligibility(valid_record(), risk_policy=policy(allowed_symbols=("GBPUSD",)),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "SYMBOL_NOT_ALLOWED")

    def test_blocked_on_invalid_direction(self):
        result = check_eligibility(valid_record(direction="SIDEWAYS"), risk_policy=policy(),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "INVALID_DIRECTION")

    def test_blocked_on_missing_prices(self):
        result = check_eligibility(valid_record(stop_price=None), risk_policy=policy(),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "INVALID_GEOMETRY")

    def test_blocked_on_invalid_stop_geometry_long(self):
        result = check_eligibility(valid_record(direction="LONG", entry_price=1.1000, stop_price=1.1050),
                                   risk_policy=policy(), account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "INVALID_STOP_GEOMETRY")

    def test_blocked_on_invalid_stop_geometry_short(self):
        result = check_eligibility(valid_record(direction="SHORT", entry_price=1.1000, stop_price=1.0950),
                                   risk_policy=policy(), account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "INVALID_STOP_GEOMETRY")

    def test_blocked_on_invalid_target_geometry(self):
        result = check_eligibility(valid_record(direction="LONG", target_price=1.0900), risk_policy=policy(),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "INVALID_TARGET_GEOMETRY")

    def test_blocked_on_stale_signal(self):
        stale_record = valid_record(signal_emitted_at=NOW - timedelta(hours=2))
        result = check_eligibility(stale_record, risk_policy=policy(max_signal_age_seconds=60.0),
                                   account_id=ACCOUNT, now_utc=NOW)
        self.assertEqual(result.reason, "STALE_SIGNAL")


class CreateExecutionIntentTests(unittest.TestCase):
    def _seeded_conn(self, signal_id="SIG1", entry_signal_hash="hash-1", **overrides) -> FakeConnection:
        conn = FakeConnection()
        fields = dict(signal_id=signal_id, strategy_id="STRAT1", strategy_version=1, strategy_ref="strat-ref",
                     instrument="EURUSD", direction="LONG", decision_time=NOW, entry_price=1.1000,
                     stop_price=1.0950, target_price=1.1100, signal_emitted_at=NOW,
                     entry_signal_hash=entry_signal_hash)
        fields.update(overrides)
        conn.seed_entry_signal(**fields)
        return conn

    def test_raises_when_entry_signal_record_is_missing(self):
        conn = FakeConnection()
        with self.assertRaises(EntrySignalRecordMissing):
            create_execution_intent(conn, signal_id="MISSING", account_id=ACCOUNT, risk_policy=policy(), now_utc=NOW)

    def test_created_on_an_eligible_signal_and_publishes_an_outbox_event(self):
        conn = self._seeded_conn()
        result = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(), now_utc=NOW)
        self.assertEqual(result.status, "CREATED")
        self.assertTrue(result.eligible)
        self.assertIsNotNone(result.execution_intent_id)

        row = conn.tables["execution_v2.execution_intent"][result.execution_intent_id]  # keyed by PK
        self.assertEqual(row["status"], "PENDING")

        outbox = list(conn.tables["platform.outbox_events"].values())
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["event_type"], "execution.intent.created.v1")
        self.assertEqual(outbox[0]["correlation_id"], "SIG1")

    def test_blocked_on_an_ineligible_signal_still_records_the_intent(self):
        conn = self._seeded_conn()
        result = create_execution_intent(conn, signal_id="SIG1", account_id="NOT_ALLOWED",
                                         risk_policy=policy(), now_utc=NOW)
        self.assertEqual(result.status, "BLOCKED")
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "ACCOUNT_NOT_ALLOWED")
        row = conn.tables["execution_v2.execution_intent"][result.execution_intent_id]  # keyed by PK
        self.assertEqual(row["status"], "BLOCKED")
        self.assertEqual(row["block_reason"], "ACCOUNT_NOT_ALLOWED")

    def test_duplicate_call_for_the_same_signal_and_account_is_idempotent(self):
        conn = self._seeded_conn()
        first = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(), now_utc=NOW)
        second = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(), now_utc=NOW)
        self.assertEqual(first.execution_intent_id, second.execution_intent_id)
        self.assertEqual(second.status, "DUPLICATE")
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 1)
        self.assertEqual(len(conn.tables["platform.outbox_events"]), 1)  # never re-published

    def test_a_second_account_for_an_already_intented_signal_never_creates_a_second_intent(self):
        # `execution_v2.execution_intent.entry_signal_id` is UNIQUE (migration 016): this V2 slice
        # is single-personal-account only (mission section 1 explicitly excludes multi-account
        # routing), so the schema itself - not just application logic - forbids a second intent
        # for the same signal under a different account, even though the two calls compute
        # different (unused) execution_intent_id candidates.
        conn = self._seeded_conn()
        first = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(), now_utc=NOW)
        other_policy = policy(allowed_accounts=(ACCOUNT, "ACC2"))
        second = create_execution_intent(conn, signal_id="SIG1", account_id="ACC2", risk_policy=other_policy, now_utc=NOW)
        self.assertEqual(first.status, "CREATED")
        self.assertEqual(second.status, "DUPLICATE")
        self.assertNotEqual(first.execution_intent_id, second.execution_intent_id)  # candidate ids still differ
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 1)  # but only one row ever lands

    def test_quarantined_on_entry_signal_hash_mismatch_and_creates_no_intent(self):
        conn = self._seeded_conn(entry_signal_hash="hash-current")
        result = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(),
                                         now_utc=NOW, claimed_entry_signal_hash="hash-stale-event-payload")
        self.assertEqual(result.status, "QUARANTINED")
        self.assertIsNone(result.execution_intent_id)
        self.assertEqual(result.reason, "EVENT_HASH_MISMATCH")
        self.assertEqual(len(conn.tables["execution_v2.execution_intent"]), 0)
        finding = list(conn.tables["execution_v2.reconciliation_finding"].values())[0]
        self.assertEqual(finding["broker_truth"], "STILL_UNKNOWN")

    def test_matching_claimed_hash_proceeds_normally(self):
        conn = self._seeded_conn(entry_signal_hash="hash-current")
        result = create_execution_intent(conn, signal_id="SIG1", account_id=ACCOUNT, risk_policy=policy(),
                                         now_utc=NOW, claimed_entry_signal_hash="hash-current")
        self.assertEqual(result.status, "CREATED")


if __name__ == "__main__":
    unittest.main()
