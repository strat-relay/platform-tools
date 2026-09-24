"""execution_v2/risk_policy_store.py: the Postgres-backed override the Console's Risk &
Execution page reads/writes (mission CLAUDE-V2-RISK-EXECUTION-CONSOLE)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from execution_v2.risk import RiskPolicyError
from execution_v2.risk_policy_store import (
    SOURCE_FILE_BASELINE, SOURCE_OVERRIDE, read_canary_status, read_effective_policy,
    write_policy_override,
)

VALID_POLICY = {
    "version": 1, "enabled": True, "max_volume": 0.01,
    "allowed_symbols": ["XAUUSD", "BTCUSD", "USDJPY", "EURUSD"],
    "allowed_accounts": ["188428665"], "allowed_strategies": ["CONTEXT_STRUCTURE_RETRACE_V1@V1"],
    "risk_per_trade": 0.005, "max_signal_age_seconds": 60, "max_daily_loss": 10,
    "max_concurrent_positions": 1, "max_concurrent_orders": 1, "max_account_exposure": 50,
    "duplicate_position_policy": "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY",
    "canary_max_new_executions": 1,
}


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._result = None

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql: str, params=()) -> None:
        upper = " ".join(sql.split()).upper()
        if "SELECT POLICY_JSON FROM EXECUTION_V2.RISK_POLICY_OVERRIDE" in upper:
            self._result = (self.conn.override,) if self.conn.override is not None else None
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_OVERRIDE"):
            self.conn.override = json.loads(params[0])
            self.conn.override_updated_by = params[1]
        elif "SELECT MAX_NEW_EXECUTIONS, CONSUMED FROM EXECUTION_V2.CANARY_STATE" in upper:
            row = self.conn.canary_rows.get(params[0])
            self._result = row
        else:
            raise AssertionError(f"unexpected SQL in fake: {sql}")

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, *, override: dict | None = None, canary_rows: dict | None = None) -> None:
        self.override = override
        self.override_updated_by = None
        self.canary_rows = canary_rows or {}
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self): return self
    def __exit__(self, *exc): return False


def connect_fn_for(conn: FakeConnection):
    def _connect(readonly: bool = False):
        return conn
    return _connect


class ReadEffectivePolicyTests(unittest.TestCase):
    def test_no_override_falls_back_to_the_file_baseline(self):
        conn = FakeConnection(override=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(VALID_POLICY))
            policy, source = read_effective_policy(connect_fn_for(conn), file_path=str(path))
        self.assertEqual(source, SOURCE_FILE_BASELINE)
        self.assertEqual(policy.risk_per_trade, 0.005)

    def test_an_existing_override_is_preferred_over_the_file(self):
        override = dict(VALID_POLICY, risk_per_trade=0.01)
        conn = FakeConnection(override=override)
        policy, source = read_effective_policy(connect_fn_for(conn), file_path="/nonexistent/should/not/be/read.json")
        self.assertEqual(source, SOURCE_OVERRIDE)
        self.assertEqual(policy.risk_per_trade, 0.01)

    def test_a_corrupt_file_baseline_with_no_override_fails_closed(self):
        conn = FakeConnection(override=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text("not json")
            with self.assertRaises(RiskPolicyError):
                read_effective_policy(connect_fn_for(conn), file_path=str(path))


class WritePolicyOverrideTests(unittest.TestCase):
    def test_a_valid_policy_is_persisted_and_echoed_back(self):
        conn = FakeConnection(override=None)
        saved = write_policy_override(VALID_POLICY, updated_by="operator@example.com", connect_fn=connect_fn_for(conn))
        self.assertTrue(conn.committed)
        self.assertEqual(saved.risk_per_trade, 0.005)
        self.assertEqual(conn.override["risk_per_trade"], 0.005)
        self.assertEqual(conn.override_updated_by, "operator@example.com")

    def test_an_invalid_policy_is_rejected_and_never_written(self):
        conn = FakeConnection(override=None)
        invalid = dict(VALID_POLICY, risk_per_trade=-1)  # must be > 0
        with self.assertRaises(RiskPolicyError):
            write_policy_override(invalid, connect_fn=connect_fn_for(conn))
        self.assertIsNone(conn.override)
        self.assertFalse(conn.committed)

    def test_missing_required_field_is_rejected_fail_closed_not_defaulted(self):
        conn = FakeConnection(override=None)
        incomplete = {k: v for k, v in VALID_POLICY.items() if k != "max_daily_loss"}
        with self.assertRaises(RiskPolicyError):
            write_policy_override(incomplete, connect_fn=connect_fn_for(conn))
        self.assertIsNone(conn.override)

    def test_an_unknown_extra_field_in_the_submission_is_never_persisted(self):
        conn = FakeConnection(override=None)
        write_policy_override(dict(VALID_POLICY, some_unexpected_field="x"), connect_fn=connect_fn_for(conn))
        self.assertNotIn("some_unexpected_field", conn.override)

    def test_enabled_false_is_accepted_and_preserved_independent_of_any_authority_concept(self):
        conn = FakeConnection(override=None)
        saved = write_policy_override(dict(VALID_POLICY, enabled=False), connect_fn=connect_fn_for(conn))
        self.assertFalse(saved.enabled)
        # Disabled policies still carry their reviewed values through - nothing about "enabled"
        # here has any notion of execution authority; that is a completely separate env var this
        # module never reads or writes.
        self.assertEqual(saved.risk_per_trade, 0.005)


class CanaryStatusTests(unittest.TestCase):
    def test_no_row_yet_reports_full_configured_capacity_as_remaining(self):
        conn = FakeConnection(canary_rows={})
        status = read_canary_status(connect_fn_for(conn), canary_key="execution:real:188428665", configured_max=1)
        self.assertEqual(status, {"max_new_executions": 1, "consumed": 0, "remaining": 1})

    def test_an_existing_row_reports_the_real_durable_counter(self):
        conn = FakeConnection(canary_rows={"execution:real:188428665": (1, 1)})
        status = read_canary_status(connect_fn_for(conn), canary_key="execution:real:188428665", configured_max=1)
        self.assertEqual(status, {"max_new_executions": 1, "consumed": 1, "remaining": 0})

    def test_remaining_never_goes_negative(self):
        conn = FakeConnection(canary_rows={"execution:real:188428665": (1, 5)})
        status = read_canary_status(connect_fn_for(conn), canary_key="execution:real:188428665", configured_max=1)
        self.assertEqual(status["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
