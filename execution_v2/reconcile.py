"""Minimum reconciliation required for safe first execution (mission section 10): "did this
attempt actually produce an order/deal?" after an ambiguous submission. Not a reconciliation
platform - one function, one append-only finding table.

The original `execution_v2.execution_result` row recording `UNKNOWN_RECONCILIATION_REQUIRED` is
never rewritten (it is immutable by trigger, and `attempt_id` is UNIQUE) - a finding is a
separate, additional fact laid alongside it, so the audit trail always shows both "what we
recorded at the time" and "what we later established." No finding here ever triggers an
automatic resubmission; that decision, if ever made, belongs to an operator, not this function.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes

from .bridge_fence_types import BridgeFence
from .ids import finding_id as _finding_id


def reconcile_attempt(conn: Any, bridge: BridgeFence, attempt_id: str,
                      now_utc: datetime | None = None) -> str:
    """Returns the `broker_truth` recorded. Queries the bridge's own ledger (over HTTP against
    the real bridge in production) - the same source of truth `submit()` itself consults - never
    guesses from PostgreSQL state alone."""
    now_utc = now_utc or datetime.now(timezone.utc)
    entry = bridge.ledger_entry(attempt_id)
    detail: dict[str, Any] = {}
    broker_order_id = None

    if entry is None:
        broker_truth = "STILL_UNKNOWN"
        detail = {"bridge_ledger": "no entry found"}
    elif entry.state == "DISPATCHED":
        response = entry.broker_response or {}
        status = response.get("status")
        if status in ("FILLED", "ACCEPTED"):
            broker_truth = "CONFIRMED_EXECUTED"
            broker_order_id = response.get("broker_order_id")
        elif status == "REJECTED":
            broker_truth = "CONFIRMED_NOT_EXECUTED"
        else:
            broker_truth = "STILL_UNKNOWN"
        detail = {"bridge_ledger_state": entry.state, "broker_response": response}
    else:
        broker_truth = "STILL_UNKNOWN"
        detail = {"bridge_ledger_state": entry.state}

    finding_id = _finding_id(attempt_id=attempt_id, queried_at=now_utc.isoformat())
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO execution_v2.reconciliation_finding
                      (finding_id, attempt_id, broker_truth, broker_order_id, detail)
                      VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                   (finding_id, attempt_id, broker_truth, broker_order_id,
                    canonical_bytes(detail).decode("utf-8")))
    conn.commit()
    return broker_truth
