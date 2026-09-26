"""Canonical operator control for explicit, generation-scoped canary windows."""
from __future__ import annotations

import uuid
from typing import Any, Callable

from postgres.db import connect, transaction


class CanaryWindowError(RuntimeError):
    pass


def read_active_canary_status(connect_fn: Callable[..., Any] = connect, *,
                              environment: str, account_id: str) -> dict[str, Any] | None:
    with connect_fn(readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT canary_key, generation, lifecycle_state, max_new_executions,
                                      consumed, created_at, closed_at, opened_by
                               FROM execution_v2.canary_state
                              WHERE environment=%s AND account_id=%s AND lifecycle_state='ACTIVE'
                              ORDER BY generation DESC""", (environment, account_id))
            rows = cur.fetchall()
    if len(rows) > 1:
        raise CanaryWindowError("multiple active canary windows")
    if not rows:
        return None
    row = rows[0]
    if len(row) == 5:  # compatibility with lightweight store fakes used by offline tests
        key, generation, state, maximum, consumed = row
        created_at = closed_at = opened_by = None
    else:
        key, generation, state, maximum, consumed, created_at, closed_at, opened_by = row
    return {"canary_key": key, "generation": int(generation), "state": state,
            "max_new_executions": int(maximum), "consumed": int(consumed),
            "remaining": max(0, int(maximum) - int(consumed)), "created_at": created_at,
            "closed_at": closed_at, "opened_by": opened_by}


def open_canary_window(*, account_id: str, environment: str, max_new_executions: int,
                       changed_by: str, connect_fn: Callable[..., Any] = connect) -> dict[str, Any]:
    if environment not in {"real", "demo"}:
        raise CanaryWindowError("environment must be real or demo")
    if not account_id or len(account_id) > 64:
        raise CanaryWindowError("explicit account_id is required")
    if max_new_executions <= 0:
        raise CanaryWindowError("max_new_executions must be greater than zero")
    if not changed_by or len(changed_by) > 200:
        raise CanaryWindowError("changed_by is required")
    conn = connect_fn(readonly=False)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM execution_v2.execution_authority WHERE authority_id='current' FOR SHARE")
            authority = cur.fetchone()
            if not authority or authority[0] != "DISABLED":
                raise CanaryWindowError("execution authority must be DISABLED")
            cur.execute("""SELECT canary_key, generation, lifecycle_state
                             FROM execution_v2.canary_state
                            WHERE environment=%s AND account_id=%s
                            ORDER BY generation DESC FOR UPDATE""", (environment, account_id))
            previous = cur.fetchall()
            active = next((row for row in previous if row[2] == "ACTIVE"), None)
            previous_key = active[0] if active else None
            if active:
                cur.execute("SELECT max_new_executions FROM execution_v2.canary_state WHERE canary_key=%s",
                            (active[0],))
                previous_limit = int(cur.fetchone()[0])
                cur.execute("""UPDATE execution_v2.canary_state
                                  SET lifecycle_state='CLOSED', closed_at=now(), updated_at=now()
                                WHERE canary_key=%s""", (active[0],))
                cur.execute("""INSERT INTO execution_v2.canary_window_change
                    (change_id, canary_key, previous_canary_key, environment, account_id,
                     generation, max_new_executions, action, changed_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'CLOSE',%s)""",
                            (str(uuid.uuid4()), active[0], None, environment, account_id,
                             int(active[1]), previous_limit, changed_by))
            generation = max((int(row[1]) for row in previous), default=0) + 1
            key = f"execution:{environment}:{account_id}:g{generation}"
            cur.execute("""INSERT INTO execution_v2.canary_state
                (canary_key, account_id, environment, generation, max_new_executions,
                 consumed, lifecycle_state, created_at, opened_by)
                VALUES (%s,%s,%s,%s,%s,0,'ACTIVE',now(),%s)""",
                        (key, account_id, environment, generation, max_new_executions, changed_by))
            cur.execute("""INSERT INTO execution_v2.canary_window_change
                (change_id, canary_key, previous_canary_key, environment, account_id,
                 generation, max_new_executions, action, changed_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)""",
                        (str(uuid.uuid4()), key, previous_key, environment, account_id,
                         generation, max_new_executions, changed_by))
    return {"canary_key": key, "generation": generation, "state": "ACTIVE",
            "max_new_executions": max_new_executions, "consumed": 0,
            "remaining": max_new_executions, "environment": environment,
            "account_id": account_id, "opened_by": changed_by}
