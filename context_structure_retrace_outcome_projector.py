"""Persist the frozen Context runner's ENTRY_ONLY outcome state.

This module is called only after the runner has finished a poll and checkpointed
its state. It does not calculate outcomes and is deliberately not imported by
the frozen bar-decision path. The runner is authoritative for every matched
signal: the database row is a projection, not an operator-owned terminal record.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Callable

from postgres.db import connect

STRATEGY_ID = "CONTEXT_STRUCTURE_RETRACE_V1"
OUTCOME_TYPE = "ENTRY_ONLY"
OUTCOME_SOURCE = STRATEGY_ID
METADATA_KEY = "context.entry_only_outcome_cutoff"
OUTCOME_SCHEMA_VERSION = "015"
ALLOWED_STATUSES = {"OPEN", "TARGET_HIT", "STOPPED", "INVALIDATED"}


class OutcomeProjectionError(RuntimeError):
    """The canonical outcome projection could not be kept consistent."""


def _cutoff_utc() -> str:
    configured = os.getenv("OUTCOME_CUTOFF_UTC")
    if configured:
        value = datetime.fromisoformat(configured.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise OutcomeProjectionError("OUTCOME_CUTOFF_UTC must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _position_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions = state.get("positions") or {}
    if isinstance(positions, dict):
        return {str(key): value for key, value in positions.items() if isinstance(value, dict)}
    return {str(row.get("economic_position_id")): row for row in positions
            if isinstance(row, dict) and row.get("economic_position_id")}


def _exit_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutcomeProjectionError("runner exit_timestamp must be a UTC epoch") from exc


def project_entry_only_outcomes(
    state: dict[str, Any],
    *,
    connect_fn: Callable[..., Any] = connect,
    environ: dict[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    """Idempotently project only signals in the configured canonical cutoff.

    The PostgreSQL EntrySignal set is the allowlist. The runner ledger is only
    consulted for a matching current position, so unrelated historical ledger
    rows can never be imported by this function.
    """
    env = os.environ if environ is None else environ
    cutoff_id = (env.get("ENTRY_OUTCOME_SIGNAL_CUTOFF_ID") or "").strip()
    if not cutoff_id:
        raise OutcomeProjectionError("ENTRY_OUTCOME_SIGNAL_CUTOFF_ID is required")
    positions = _position_map(state)
    counts = {"matched": 0, "projected": 0, "unchanged": 0, "unmatched": 0}
    now = clock or (lambda: datetime.now(timezone.utc))

    with connect_fn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s",
                        (OUTCOME_SCHEMA_VERSION,))
            if cur.fetchone() is None:
                raise OutcomeProjectionError("PostgreSQL migration 015 is required")

            cur.execute("SELECT value FROM platform.system_metadata WHERE key = %s FOR UPDATE",
                        (METADATA_KEY,))
            metadata_row = cur.fetchone()
            if metadata_row is None:
                cutoff = _cutoff_utc()
                cur.execute(
                    """INSERT INTO platform.system_metadata(key, value, updated_at)
                       VALUES (%s, jsonb_build_object('cutoff_utc', %s::timestamptz,
                           'signal_cutoff_id', %s::text, 'strategy_id', %s::text,
                           'outcome_type', %s::text), %s)
                       ON CONFLICT (key) DO NOTHING""",
                    (METADATA_KEY, cutoff, cutoff_id, STRATEGY_ID, OUTCOME_TYPE, now()),
                )
                cur.execute("SELECT value FROM platform.system_metadata WHERE key = %s FOR UPDATE",
                            (METADATA_KEY,))
                metadata_row = cur.fetchone()
            metadata = metadata_row[0]
            if isinstance(metadata, str):
                import json
                metadata = json.loads(metadata)
            if metadata.get("signal_cutoff_id") != cutoff_id or metadata.get("strategy_id") != STRATEGY_ID:
                raise OutcomeProjectionError("stored outcome cutoff does not match configured canonical cutoff")

            cur.execute(
                """SELECT signal_id, economic_position_id, entry_opportunity_id
                   FROM strategy.entry_signals
                   WHERE strategy_id = %s AND cutoff_id = %s
                   ORDER BY signal_id""",
                (STRATEGY_ID, cutoff_id),
            )
            signals = cur.fetchall()
            for signal_id, position_id, opportunity_id in signals:
                position = positions.get(str(position_id)) if position_id else None
                if not position or (opportunity_id and
                                    str(position.get("entry_opportunity_id")) != str(opportunity_id)):
                    counts["unmatched"] += 1
                    continue
                status = str(position.get("status") or "").upper()
                if status not in ALLOWED_STATUSES:
                    raise OutcomeProjectionError(f"unsupported ENTRY_ONLY status {status!r} for {signal_id}")
                realized_r = position.get("realized_R")
                exit_at = _exit_timestamp(position.get("exit_timestamp"))
                if status == "OPEN" and (realized_r is not None or exit_at is not None):
                    raise OutcomeProjectionError(f"OPEN position {signal_id} unexpectedly has exit data")
                if status not in {"OPEN", "INVALIDATED"} and (realized_r is None or exit_at is None):
                    raise OutcomeProjectionError(f"closed position {signal_id} is missing exit data")
                if status == "INVALIDATED" and exit_at is None:
                    raise OutcomeProjectionError(f"invalidated position {signal_id} is missing exit data")

                cur.execute(
                    """INSERT INTO strategy.entry_signal_outcomes
                           (signal_id, outcome_type, status, realized_r, exit_timestamp, source)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (signal_id) DO UPDATE SET
                           status = EXCLUDED.status,
                           realized_r = EXCLUDED.realized_r,
                           exit_timestamp = EXCLUDED.exit_timestamp,
                           updated_at = %s
                       WHERE (strategy.entry_signal_outcomes.status,
                              strategy.entry_signal_outcomes.realized_r,
                              strategy.entry_signal_outcomes.exit_timestamp,
                              strategy.entry_signal_outcomes.outcome_type,
                              strategy.entry_signal_outcomes.source)
                             IS DISTINCT FROM
                             (EXCLUDED.status, EXCLUDED.realized_r, EXCLUDED.exit_timestamp,
                              EXCLUDED.outcome_type, EXCLUDED.source)
                       RETURNING signal_id""",
                    (signal_id, OUTCOME_TYPE, status, realized_r, exit_at, OUTCOME_SOURCE, now()),
                )
                changed = cur.fetchone()
                if changed:
                    counts["projected"] += 1
                else:
                    cur.execute(
                        """SELECT outcome_type, status, realized_r, exit_timestamp, source
                           FROM strategy.entry_signal_outcomes WHERE signal_id = %s""",
                        (signal_id,),
                    )
                    stored = cur.fetchone()
                    wanted = (OUTCOME_TYPE, status, realized_r, exit_at, OUTCOME_SOURCE)
                    same_realized_r = (
                        stored is not None
                        and ((stored[2] is None and realized_r is None)
                             or (stored[2] is not None and realized_r is not None
                                 and math.isclose(float(stored[2]), float(realized_r),
                                                  rel_tol=1e-12, abs_tol=1e-12)))
                    )
                    same_exit_time = (
                        stored is not None
                        and ((stored[3] is None and exit_at is None)
                             or (stored[3] is not None and exit_at is not None
                                 and stored[3].astimezone(timezone.utc) == exit_at))
                    )
                    matches = (stored is not None and stored[0] == wanted[0]
                               and stored[1] == wanted[1] and same_realized_r
                               and same_exit_time and stored[4] == wanted[4])
                    if not matches:
                        raise OutcomeProjectionError(f"outcome projection did not converge for {signal_id}")
                    counts["unchanged"] += 1
                counts["matched"] += 1
        conn.commit()
    return counts
