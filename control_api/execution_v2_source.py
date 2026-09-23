"""Canonical PostgreSQL read path for `/executions`, behind an explicit opt-in.

`control_api/app.py` keeps the legacy filesystem route by default. Setting
`CONTROL_API_EXECUTION_SOURCE=canonical` selects this read-only projection; no legacy execution
authority is restored and no write path is exposed.

## Integration point (for whoever wires this in, under its own separately-authorized change)

`control_api/app.py`'s `_handle_get` currently has one branch for `resource == "executions"`
(and one for `"executions"`/`identifier == "metrics"`) that unconditionally reads the legacy
sources. The prepared integration is:

    if resource == "executions" and os.getenv("CONTROL_API_EXECUTION_SOURCE") == "canonical":
        from .execution_v2_source import read_execution_v2_summary
        conn = self._execution_v2_connection()  # a new, lazily-created PostgreSQL connection
        return 200, self._envelope(read_execution_v2_summary(conn, execution_authority_mode=...))
    if resource == "executions":
        ... existing legacy-source code, UNCHANGED ...

`CONTROL_API_EXECUTION_SOURCE` defaults unset (legacy path), so current production behavior is
unchanged unless the explicit switch is set.

## Truthful inactive-state contract (mission section 16)

`read_execution_v2_summary()` never claims execution capability that does not exist: `enabled` is
read directly from the same `EXECUTION_AUTHORITY_MODE` value the runtime itself uses
(`migration.flags`), not inferred from row counts; a PostgreSQL instance with zero execution_v2
rows AND `EXECUTION_AUTHORITY_MODE=DISABLED` produces
`{"enabled": false, "intents": 0, "attempts": 0, "results": 0, ...}` - a truthful "nothing has
happened and nothing can happen" statement, matching what this mission's own isolated proof
produces before any signal is ever processed.
"""
from __future__ import annotations

from typing import Any


def read_execution_v2_summary(conn: Any, *, execution_authority_mode: str, account_id: str | None = None) -> dict[str, Any]:
    """Read-only. Never writes. `conn` is any object exposing psycopg's `cursor()`/`fetchall()`
    surface (the same interface `execution_v2/fakes.py::FakeConnection` and a real `psycopg.
    Connection` both satisfy, so this function is testable without PostgreSQL)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT status, count(*) FROM execution_v2.execution_intent
                      WHERE %(account_id)s IS NULL OR account_id = %(account_id)s
                      GROUP BY status""", {"account_id": account_id})
        intent_status_counts = dict(cur.fetchall())

        cur.execute("""SELECT state, count(*) FROM execution_v2.execution_attempt
                      WHERE %(account_id)s IS NULL OR account_id = %(account_id)s
                      GROUP BY state""", {"account_id": account_id})
        attempt_state_counts = dict(cur.fetchall())

        cur.execute("""SELECT outcome, count(*) FROM execution_v2.execution_result
                      WHERE %(account_id)s IS NULL OR account_id = %(account_id)s
                      GROUP BY outcome""", {"account_id": account_id})
        result_outcome_counts = dict(cur.fetchall())

        cur.execute("SELECT count(*) FROM execution_v2.reconciliation_finding")
        (reconciliation_findings,) = cur.fetchone() or (0,)

    return {
        "source": "execution_v2_canonical",
        "enabled": execution_authority_mode == "ENABLED",
        "execution_authority_mode": execution_authority_mode,
        "account_id": account_id,
        "intents": sum(intent_status_counts.values()),
        "intents_by_status": intent_status_counts,
        "attempts": sum(attempt_state_counts.values()),
        "attempts_by_state": attempt_state_counts,
        "results": sum(result_outcome_counts.values()),
        "results_by_outcome": result_outcome_counts,
        "reconciliation_findings_open": reconciliation_findings,
        "broker_writes": execution_authority_mode == "ENABLED",  # matches HealthState.execution_status()
    }
