"""Canonical, idempotent Liquidity outcome projection helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from postgres.db import connect

STRATEGY_ID = "LIQUIDITY_DISPLACEMENT_SCALP_V1"
OUTCOME_TYPE = "LIQUIDITY_ENTRY"


def project_liquidity_outcome(signal_id: str, *, status: str, realized_r: float | None,
                              exit_timestamp: str | None, connect_fn: Callable[..., Any] = connect) -> bool:
    """Project one terminal/open result; terminal rows are immutable."""
    allowed = {"OPEN", "TARGET_HIT", "STOPPED", "TIME_EXIT", "EXPIRED", "INVALIDATED"}
    if status not in allowed:
        raise ValueError(f"unsupported Liquidity outcome: {status}")
    if status == "OPEN":
        if realized_r is not None or exit_timestamp is not None:
            raise ValueError("OPEN outcome cannot have realized_r or exit_timestamp")
        exit_at = None
    else:
        if exit_timestamp is None:
            raise ValueError("terminal outcome requires exit_timestamp")
        exit_at = datetime.fromisoformat(exit_timestamp.replace("Z", "+00:00"))
        if exit_at.tzinfo is None:
            raise ValueError("exit_timestamp must be timezone-aware")
        if status in {"TARGET_HIT", "STOPPED", "TIME_EXIT"} and realized_r is None:
            raise ValueError("price terminal outcome requires realized_r")
        if status in {"EXPIRED", "INVALIDATED"} and realized_r is not None:
            raise ValueError("expired/invalidated outcome cannot have realized_r")
    with connect_fn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_id FROM strategy.entry_signals WHERE signal_id = %s", (signal_id,))
            row = cur.fetchone()
            if row is None or row[0] != STRATEGY_ID:
                raise ValueError("signal is not a Liquidity signal")
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                (signal_id, outcome_type, status, realized_r, exit_timestamp, source)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    status = EXCLUDED.status, realized_r = EXCLUDED.realized_r,
                    exit_timestamp = EXCLUDED.exit_timestamp, updated_at = now()
                WHERE strategy.entry_signal_outcomes.status = 'OPEN'
                RETURNING signal_id""", (signal_id, OUTCOME_TYPE, status, realized_r, exit_at, STRATEGY_ID))
            changed = cur.fetchone() is not None
        conn.commit()
    return changed
