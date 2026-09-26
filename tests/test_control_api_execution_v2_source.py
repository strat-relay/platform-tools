"""Proof for the prepared (not wired, not deployed) canonical execution API read path (mission
section 16): truthful inactive-state representation with zero rows, and correct aggregation once
rows exist - using a tiny local fake cursor matching psycopg's dict-param GROUP BY query surface
(deliberately not importing execution_v2.fakes.FakeConnection, which does not model GROUP BY -
this module's own substrate stays independently minimal, matching its "additive, separate module"
design).
"""
from __future__ import annotations

import unittest
from typing import Any

from control_api.execution_v2_source import read_execution_v2_summary


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self._result: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        upper = " ".join(sql.split()).upper()
        account_id = (params or {}).get("account_id") if isinstance(params, dict) else None
        if "EXECUTION_V2.EXECUTION_INTENT" in upper:
            source = self.conn.intents
        elif "EXECUTION_V2.EXECUTION_ATTEMPT" in upper:
            source = self.conn.attempts
        elif "EXECUTION_V2.EXECUTION_RESULT" in upper:
            source = self.conn.results
        elif "EXECUTION_V2.RECONCILIATION_FINDING" in upper:
            self._result = [(len(self.conn.findings),)]
            return
        else:
            raise AssertionError(f"unexpected query: {sql[:80]}")
        filtered = [r for r in source if account_id is None or r["account_id"] == account_id]
        counts: dict[str, int] = {}
        key = "status" if "EXECUTION_INTENT" in upper else ("state" if "EXECUTION_ATTEMPT" in upper else "outcome")
        for row in filtered:
            counts[row[key]] = counts.get(row[key], 0) + 1
        self._result = list(counts.items())

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self) -> None:
        self.intents: list[dict] = []
        self.attempts: list[dict] = []
        self.results: list[dict] = []
        self.findings: list[dict] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


class ReadExecutionV2SummaryTests(unittest.TestCase):
    def test_truthfully_reports_disabled_and_empty_when_nothing_has_ever_run(self):
        conn = _FakeConn()
        summary = read_execution_v2_summary(conn, execution_authority_mode="DISABLED")
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["broker_writes"])
        self.assertEqual(summary["intents"], 0)
        self.assertEqual(summary["attempts"], 0)
        self.assertEqual(summary["results"], 0)
        self.assertEqual(summary["reconciliation_findings_open"], 0)

    def test_enabled_flag_is_read_from_the_mode_never_inferred_from_row_counts(self):
        conn = _FakeConn()
        conn.intents.append({"account_id": "ACC1", "status": "CREATED"})
        summary = read_execution_v2_summary(conn, execution_authority_mode="DISABLED")
        self.assertFalse(summary["enabled"])  # rows exist, but the mode alone controls "enabled"
        self.assertEqual(summary["intents"], 1)

    def test_aggregates_by_status_state_and_outcome(self):
        conn = _FakeConn()
        conn.intents = [{"account_id": "ACC1", "status": "CREATED"}, {"account_id": "ACC1", "status": "BLOCKED"},
                        {"account_id": "ACC1", "status": "CREATED"}]
        conn.attempts = [{"account_id": "ACC1", "state": "CONFIRMED"}]
        conn.results = [{"account_id": "ACC1", "outcome": "FILLED"}, {"account_id": "ACC1", "outcome": "REJECTED"}]
        summary = read_execution_v2_summary(conn, execution_authority_mode="ENABLED", account_id="ACC1")
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["intents_by_status"], {"CREATED": 2, "BLOCKED": 1})
        self.assertEqual(summary["attempts_by_state"], {"CONFIRMED": 1})
        self.assertEqual(summary["results_by_outcome"], {"FILLED": 1, "REJECTED": 1})

    def test_account_filter_excludes_other_accounts(self):
        conn = _FakeConn()
        conn.intents = [{"account_id": "ACC1", "status": "CREATED"}, {"account_id": "ACC2", "status": "CREATED"}]
        summary = read_execution_v2_summary(conn, execution_authority_mode="DISABLED", account_id="ACC1")
        self.assertEqual(summary["intents"], 1)


if __name__ == "__main__":
    unittest.main()
