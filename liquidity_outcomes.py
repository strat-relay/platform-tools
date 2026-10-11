"""Canonical, idempotent Liquidity outcome projection helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from postgres.db import connect
from outcome_resolver import persist_outcome_row

STRATEGY_ID = "LIQUIDITY_DISPLACEMENT_SCALP_V1"
OUTCOME_TYPE = "LIQUIDITY_ENTRY"


def project_liquidity_outcome(signal_id: str, *, status: str, realized_r: float | None,
                              exit_timestamp: str | None, conn: Any | None = None,
                              connect_fn: Callable[..., Any] = connect) -> bool:
    """Project one terminal/open result; terminal rows are immutable.

    A caller may provide ``conn`` when this projection is part of a larger
    transaction.  That connection is borrowed: this helper neither commits
    nor closes it.  Without ``conn``, the helper creates a connection through
    ``connect_fn`` and owns its transaction and cleanup.

    ``connect_fn`` is retained for isolated callers and tests.  It is not used
    when a borrowed connection is supplied, so ownership cannot be inferred
    from a callable returning an existing connection.
    """
    if conn is not None and connect_fn is not connect:
        raise ValueError("provide either conn or connect_fn, not both")
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
    def project(borrowed_conn: Any) -> bool:
        with borrowed_conn.cursor() as cur:
            cur.execute("SELECT strategy_id FROM strategy.entry_signals WHERE signal_id = %s", (signal_id,))
            row = cur.fetchone()
            if row is None or row[0] != STRATEGY_ID:
                raise ValueError("signal is not a Liquidity signal")
            return persist_outcome_row(
                cur, signal_id=signal_id, outcome_type=OUTCOME_TYPE, status=status,
                realized_r=realized_r, exit_timestamp=exit_at, source=STRATEGY_ID,
                updated_at=datetime.now(timezone.utc),
            )

    if conn is not None:
        return project(conn)

    with connect_fn() as owned_conn:
        changed = project(owned_conn)
        owned_conn.commit()
        return changed
