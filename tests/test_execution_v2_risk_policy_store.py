"""execution_v2/risk_policy_store.py: the canonical relational PostgreSQL authority for the V2
risk policy (mission CLAUDE-V2-DB-AUTHORITY-INTEGRATION, platform commit 7ecabba "Move V2 risk
policy authority to PostgreSQL"). Exercises the real read/write SQL shape end-to-end against a
fake matching execution_v2.risk_policy + its risk_policy_allowed_{account,strategy,symbol} child
tables - complementing tests/test_v2_risk_revision_conflict.py's mock-based coverage of
platform_api/v2_risk.py's own status-code handling, which patches these functions out entirely
and so never proves the SQL itself still matches what this module actually sends.
"""
from __future__ import annotations

import unittest

from execution_v2.risk import RiskPolicyError, RiskPolicyRevisionConflict
from execution_v2.risk_policy_store import (
    SOURCE_POSTGRES, persist_policy, read_canary_status, read_effective_policy,
    read_effective_policy_record, write_policy_override,
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

# Column order _read_row's SELECT and persist_policy's INSERT both use, after policy_id/revision.
_SCALAR_COLUMNS = ("version", "enabled", "risk_per_trade", "max_volume", "max_signal_age_seconds",
                   "max_daily_loss", "max_concurrent_positions", "max_concurrent_orders",
                   "max_account_exposure", "duplicate_position_policy", "canary_max_new_executions")


class FakeCursor:
    """Matches the exact SQL text risk_policy_store.py issues (see that module's `_read_row` and
    `persist_policy`) - not a generic ORM-style fake. A future edit that changes that SQL's shape
    should make this fake fail loudly (AssertionError: unexpected SQL), not silently pass."""

    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._result: tuple | None = None
        self._results: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT POLICY_ID, REVISION, VERSION"):
            row = self.conn.row
            self._result = None if row is None else (
                row["policy_id"], row["revision"], *[row[c] for c in _SCALAR_COLUMNS], row["updated_at"])
        elif upper.startswith("SELECT ACCOUNT_ID FROM"):
            self._results = [(a,) for a in sorted(self.conn.allowed_accounts)]
        elif upper.startswith("SELECT STRATEGY_REF FROM"):
            self._results = [(s,) for s in sorted(self.conn.allowed_strategies)]
        elif upper.startswith("SELECT SYMBOL FROM"):
            self._results = [(s,) for s in sorted(self.conn.allowed_symbols)]
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY "):
            (revision, version, enabled, risk_per_trade, max_volume, max_signal_age_seconds,
             max_daily_loss, max_concurrent_positions, max_concurrent_orders, max_account_exposure,
             duplicate_position_policy, canary_max_new_executions, updated_by) = params
            self.conn.row = {
                "policy_id": "current", "revision": revision, "version": version, "enabled": enabled,
                "risk_per_trade": risk_per_trade, "max_volume": max_volume,
                "max_signal_age_seconds": max_signal_age_seconds, "max_daily_loss": max_daily_loss,
                "max_concurrent_positions": max_concurrent_positions,
                "max_concurrent_orders": max_concurrent_orders, "max_account_exposure": max_account_exposure,
                "duplicate_position_policy": duplicate_position_policy,
                "canary_max_new_executions": canary_max_new_executions, "updated_at": None,
            }
            self.conn.updated_by = updated_by
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_ACCOUNT"):
            self.conn.allowed_accounts = set()
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_STRATEGY"):
            self.conn.allowed_strategies = set()
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_SYMBOL"):
            self.conn.allowed_symbols = set()
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_ACCOUNT"):
            self.conn.allowed_accounts.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_STRATEGY"):
            self.conn.allowed_strategies.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_SYMBOL"):
            self.conn.allowed_symbols.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_CHANGE"):
            self.conn.changes.append(params)
        elif "FROM EXECUTION_V2.CANARY_STATE" in upper:
            self._result = self.conn.canary_rows.get(params[0])
        else:
            raise AssertionError(f"unexpected SQL in fake: {sql}")

    def fetchone(self) -> tuple | None:
        return self._result

    def fetchall(self) -> list[tuple]:
        return self._results


class FakeConnection:
    def __init__(self, *, row: dict | None = None, allowed_accounts: set | None = None,
                allowed_strategies: set | None = None, allowed_symbols: set | None = None,
                canary_rows: dict | None = None) -> None:
        self.row = row
        self.allowed_accounts = allowed_accounts or set()
        self.allowed_strategies = allowed_strategies or set()
        self.allowed_symbols = allowed_symbols or set()
        self.canary_rows = canary_rows or {}
        self.changes: list[tuple] = []
        self.updated_by: str | None = None
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def connect_fn_for(conn: FakeConnection):
    def _connect(readonly: bool = False):
        return conn
    return _connect


def seeded_row(**overrides) -> dict:
    row = {
        "policy_id": "current", "revision": 3, "version": VALID_POLICY["version"],
        "enabled": VALID_POLICY["enabled"], "risk_per_trade": VALID_POLICY["risk_per_trade"],
        "max_volume": VALID_POLICY["max_volume"],
        "max_signal_age_seconds": VALID_POLICY["max_signal_age_seconds"],
        "max_daily_loss": VALID_POLICY["max_daily_loss"],
        "max_concurrent_positions": VALID_POLICY["max_concurrent_positions"],
        "max_concurrent_orders": VALID_POLICY["max_concurrent_orders"],
        "max_account_exposure": VALID_POLICY["max_account_exposure"],
        "duplicate_position_policy": VALID_POLICY["duplicate_position_policy"],
        "canary_max_new_executions": VALID_POLICY["canary_max_new_executions"],
        "updated_at": None,
    }
    row.update(overrides)
    return row


def seeded_conn(**row_overrides) -> FakeConnection:
    return FakeConnection(
        row=seeded_row(**row_overrides),
        allowed_accounts=set(VALID_POLICY["allowed_accounts"]),
        allowed_strategies=set(VALID_POLICY["allowed_strategies"]),
        allowed_symbols=set(VALID_POLICY["allowed_symbols"]),
    )


class ReadEffectivePolicyTests(unittest.TestCase):
    def test_reads_the_canonical_relational_row_and_its_child_tables(self):
        conn = seeded_conn()
        policy, meta = read_effective_policy_record(connect_fn_for(conn))
        self.assertEqual(meta["source"], SOURCE_POSTGRES)
        self.assertEqual(meta["revision"], 3)
        self.assertEqual(policy.risk_per_trade, 0.005)
        self.assertEqual(set(policy.allowed_symbols), {"XAUUSD", "BTCUSD", "USDJPY", "EURUSD"})

    def test_read_effective_policy_thin_wrapper_returns_source_only(self):
        conn = seeded_conn()
        policy, source = read_effective_policy(connect_fn_for(conn))
        self.assertEqual(source, SOURCE_POSTGRES)
        self.assertEqual(policy.risk_per_trade, 0.005)

    def test_a_missing_canonical_row_fails_closed_never_falls_back_to_a_file(self):
        conn = FakeConnection(row=None)
        with self.assertRaises(RiskPolicyError):
            read_effective_policy_record(connect_fn_for(conn))


class PersistPolicyTests(unittest.TestCase):
    def test_a_valid_policy_is_persisted_with_a_revision_bump_and_echoed_back(self):
        conn = seeded_conn(revision=3)
        policy, meta = persist_policy(dict(VALID_POLICY, risk_per_trade=0.01), expected_revision=3,
                                      updated_by="operator@example.com", connect_fn=connect_fn_for(conn))
        self.assertTrue(conn.committed)
        self.assertEqual(policy.risk_per_trade, 0.01)
        self.assertEqual(meta["revision"], 4)
        self.assertEqual(conn.row["revision"], 4)
        self.assertEqual(conn.row["risk_per_trade"], 0.01)
        self.assertEqual(conn.updated_by, "operator@example.com")

    def test_write_policy_override_is_the_persist_policy_thin_wrapper(self):
        conn = seeded_conn(revision=3)
        saved = write_policy_override(dict(VALID_POLICY, risk_per_trade=0.02), expected_revision=3,
                                      connect_fn=connect_fn_for(conn))
        self.assertEqual(saved.risk_per_trade, 0.02)
        self.assertEqual(conn.row["revision"], 4)

    def test_a_stale_expected_revision_raises_revision_conflict_and_never_commits(self):
        conn = seeded_conn(revision=5)
        with self.assertRaises(RiskPolicyRevisionConflict):
            persist_policy(VALID_POLICY, expected_revision=3, connect_fn=connect_fn_for(conn))
        self.assertFalse(conn.committed)
        self.assertTrue(conn.rolled_back)
        self.assertEqual(conn.row["revision"], 5)  # untouched

    def test_an_invalid_policy_is_rejected_before_any_write(self):
        conn = seeded_conn(revision=3)
        invalid = dict(VALID_POLICY, risk_per_trade=-1)  # must be > 0
        with self.assertRaises(RiskPolicyError):
            persist_policy(invalid, expected_revision=3, connect_fn=connect_fn_for(conn))
        self.assertFalse(conn.committed)
        self.assertEqual(conn.row["revision"], 3)  # untouched

    def test_allowed_symbols_are_fully_replaced_not_merged(self):
        conn = seeded_conn(revision=3)
        persist_policy(dict(VALID_POLICY, allowed_symbols=["XAUUSD"]), expected_revision=3,
                       connect_fn=connect_fn_for(conn))
        self.assertEqual(conn.allowed_symbols, {"XAUUSD"})

    def test_records_a_risk_policy_change_row_with_only_the_actually_changed_fields(self):
        conn = seeded_conn(revision=3)
        persist_policy(dict(VALID_POLICY, risk_per_trade=0.02), expected_revision=3,
                       updated_by="operator@example.com", connect_fn=connect_fn_for(conn))
        self.assertEqual(len(conn.changes), 1)
        _change_id, previous_revision, new_revision, changed_fields_json, changed_by = conn.changes[0]
        self.assertEqual(previous_revision, 3)
        self.assertEqual(new_revision, 4)
        self.assertIn('"risk_per_trade": 0.02', changed_fields_json)
        self.assertEqual(changed_by, "operator@example.com")


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
