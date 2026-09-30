"""ManagedTrade terminal lifecycle: OPEN -> CLOSED from the strategy's canonical outcome
(migration 026).

The strategy owns trade outcome. For Context Structure Retrace that is
`strategy.entry_signal_outcomes` (status TARGET_HIT | STOPPED, with exit_timestamp and
realized_r). This module never evaluates prices, stops or targets: a trade is closed only when
that canonical row is terminal. A trade whose outcome is OPEN or missing stays OPEN.

Closing sets `managed_trade.state = 'CLOSED'` and appends one
`managed_trade_lifecycle_event` (reason STRATEGY_OUTCOME). Both writes are guarded
(`WHERE state = 'OPEN'`, one event per trade/new_state), so reconciliation is idempotent.
Observations, decisions and publication rows are never touched.

Broker identity plays no part here: virtual and broker-backed trades close the same way.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from postgres.db import transaction

# INVALIDATED: an operator invalidated the signal (migration 035); it carries no realized R.
TERMINAL_OUTCOMES = ("TARGET_HIT", "STOPPED", "TIME_EXIT", "INVALIDATED")
REASON_STRATEGY_OUTCOME = "STRATEGY_OUTCOME"


@dataclass(frozen=True)
class LifecycleTransition:
    managed_trade_id: str
    entry_signal_id: str
    strategy_outcome: str
    exit_timestamp: Any
    realized_r: Any
    lifecycle_event_id: str


def lifecycle_event_id(*, managed_trade_id: str, new_state: str) -> str:
    digest = hashlib.sha256(f"{managed_trade_id}|{new_state}".encode("utf-8")).hexdigest()
    return f"MTLE_{digest[:24]}"


def close_terminal_trades(conn: Any, *, now_utc: datetime,
                          managed_trade_id: str | None = None) -> list[LifecycleTransition]:
    """Close every OPEN ManagedTrade (or just `managed_trade_id`) whose EntrySignal has a terminal
    canonical strategy outcome. Runs inside the caller's transaction; see
    `reconcile_strategy_outcomes` for the standalone, committing form."""
    sql = """SELECT mt.managed_trade_id, mt.entry_signal_id, o.status, o.exit_timestamp, o.realized_r, o.source
             FROM trade_management.managed_trade mt
             JOIN strategy.entry_signal_outcomes o ON o.signal_id = mt.entry_signal_id
            WHERE mt.state = 'OPEN' AND o.status IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'INVALIDATED')"""
    params: tuple[Any, ...] = ()
    if managed_trade_id is not None:
        sql += " AND mt.managed_trade_id = %s"
        params = (managed_trade_id,)
    sql += " ORDER BY mt.managed_trade_id FOR UPDATE OF mt SKIP LOCKED"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        candidates = cur.fetchall()

    transitioned_at = now_utc.astimezone(timezone.utc)
    transitions: list[LifecycleTransition] = []
    for trade_id, signal_id, outcome, exit_timestamp, realized_r, source in candidates:
        with conn.cursor() as cur:
            cur.execute("""UPDATE trade_management.managed_trade SET state = 'CLOSED'
                          WHERE managed_trade_id = %s AND state = 'OPEN'""", (trade_id,))
            if cur.rowcount != 1:
                continue  # closed concurrently; the other writer recorded the event
            event_id = lifecycle_event_id(managed_trade_id=trade_id, new_state="CLOSED")
            cur.execute("""INSERT INTO trade_management.managed_trade_lifecycle_event
                (lifecycle_event_id, managed_trade_id, entry_signal_id, previous_state, new_state, reason,
                 strategy_outcome, outcome_source, exit_timestamp, realized_r, transitioned_at)
                VALUES (%s, %s, %s, 'OPEN', 'CLOSED', %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING""",
                        (event_id, trade_id, signal_id, REASON_STRATEGY_OUTCOME, outcome, source,
                         exit_timestamp, realized_r, transitioned_at))
        transitions.append(LifecycleTransition(trade_id, signal_id, outcome, exit_timestamp, realized_r, event_id))
    return transitions


def reconcile_strategy_outcomes(conn: Any, *, now_utc: datetime | None = None) -> list[LifecycleTransition]:
    """Standalone reconciliation (own transaction). Used before every observation work
    selection, and usable once over existing history."""
    with transaction(conn):
        return close_terminal_trades(conn, now_utc=now_utc or datetime.now(timezone.utc))
