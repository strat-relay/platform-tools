"""Operator-controlled Trade Manager mode: OFF | SHADOW | LIVE (migrations 024, 036).

The runtime reads the mode on its own connection at every unit of work (each opening event,
each observation tick, each decision message), so a change takes effect without a restart.
Reads fail closed: the runtime does no work when the mode is OFF, missing, invalid or
unreadable; the operator read reports such a row as OFF with an error.

OFF and SHADOW have no broker effects. LIVE evaluates exactly like SHADOW; the Trade Manager itself
still never talks to a broker - the execution plane (execution_v2.management) acts on its decisions
for platform positions only while the mode is LIVE and execution authority is ENABLED.
"""
from __future__ import annotations

import uuid
from typing import Any

from postgres.db import transaction

OFF = "OFF"
SHADOW = "SHADOW"
LIVE = "LIVE"
MODES = (OFF, SHADOW, LIVE)
SKIP_REASON_OFF = "TRADE_MANAGER_OFF"


class TradeManagerModeError(RuntimeError):
    """Invalid mode request or stale revision."""


class TradeManagerModeUnavailable(RuntimeError):
    """The mode row could not be read or is invalid. Inside the runtime this fails the unit of
    work (rolled back, redelivered later) - it is never recorded as a permanent skip."""


def _select_mode(conn: Any) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT mode, revision, updated_at, updated_by
                      FROM trade_management.trade_manager_mode WHERE mode_id = 'current'""")
        row = cur.fetchone()
    return dict(zip(("mode", "revision", "updated_at", "updated_by"), row)) if row else None


def current_mode(conn: Any) -> str:
    """Strict read for the runtime, on the caller's connection and inside its transaction."""
    record = _select_mode(conn)
    if record is None or record["mode"] not in MODES:
        raise TradeManagerModeUnavailable("trade manager mode row missing or invalid")
    return record["mode"]


def read_mode_record(conn: Any) -> dict[str, Any]:
    """Lenient read for operator display: anything unreadable is reported as OFF with an error."""
    try:
        record = _select_mode(conn)
    except Exception:
        return {"mode": OFF, "revision": 0, "error": "MODE_UNAVAILABLE"}
    if record is None:
        return {"mode": OFF, "revision": 0, "error": "MODE_ROW_MISSING"}
    if record["mode"] not in MODES:
        record.update(mode=OFF, error="MODE_INVALID")
    return record


def set_mode(conn: Any, mode: str, *, expected_revision: int, changed_by: str | None,
             reason: str | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise TradeManagerModeError("mode must be OFF, SHADOW or LIVE")
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("""SELECT mode, revision FROM trade_management.trade_manager_mode
                          WHERE mode_id = 'current' FOR UPDATE""")
            row = cur.fetchone()
            if row is None:
                raise TradeManagerModeError("canonical trade manager mode row is missing")
            previous_mode, previous_revision = row
            if int(previous_revision) != int(expected_revision):
                raise TradeManagerModeError("stale trade manager mode revision")
            revision = int(previous_revision) + 1
            cur.execute("""UPDATE trade_management.trade_manager_mode
                          SET mode = %s, revision = %s, updated_at = now(), updated_by = %s
                          WHERE mode_id = 'current'""", (mode, revision, changed_by))
            cur.execute("""INSERT INTO trade_management.trade_manager_mode_change
                (change_id, mode_id, previous_mode, new_mode, previous_revision, new_revision,
                 changed_by, reason)
                VALUES (%s, 'current', %s, %s, %s, %s, %s, %s)""",
                        (str(uuid.uuid4()), previous_mode, mode, previous_revision, revision,
                         changed_by, reason))
    return {"mode": mode, "revision": revision, "previous_mode": previous_mode, "updated_by": changed_by}
