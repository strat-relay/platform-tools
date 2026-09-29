"""Operator invalidation of stale strategy signals (migration 035).

Marks the canonical outcome INVALIDATED (no realized R, exit time = now, excluded from win rate and
expectancy) for every signal decided before `--before` (default: today, 00:00 UTC) whose outcome is
still OPEN or missing, records who/why in strategy.entry_signal_outcome_override (append-only), and
closes their OPEN managed trades through the normal Trade Manager lifecycle, in one transaction.

Never touches signals decided on or after the cutoff, never overwrites a terminal outcome, and
never writes to the broker: an open broker position keeps its own stop/target.
Dry run by default:

    python -m scripts.invalidate_stale_signals --reason "..." --operator NAME [--before 2026-09-27] [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTCOME_TYPE_BY_STRATEGY = {"CONTEXT_STRUCTURE_RETRACE_V1": "ENTRY_ONLY",
                            "LIQUIDITY_DISPLACEMENT_SCALP_V1": "LIQUIDITY_ENTRY"}


def override_id(signal_id: str) -> str:
    return "OVR_" + hashlib.sha256(f"{signal_id}|INVALIDATED".encode()).hexdigest()[:24]


def invalidate(connect_fn: Callable[[], Any], *, before: datetime, reason: str, operator: str,
               apply: bool, now: datetime | None = None) -> dict[str, Any]:
    from trade_management.lifecycle import close_terminal_trades
    now = now or datetime.now(timezone.utc)
    if before > now:
        raise ValueError("--before must not be in the future")
    report: dict[str, Any] = {"dry_run": not apply, "before": before.isoformat(), "invalidated": {},
                              "skipped_unsupported_strategy": [], "managed_trades_closed": 0}
    with connect_fn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT s.signal_id, s.strategy_id, o.status
                           FROM strategy.entry_signals s
                           LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
                           WHERE s.decision_time < %s AND (o.status = 'OPEN' OR o.signal_id IS NULL)
                           ORDER BY s.decision_time, s.signal_id
                           FOR UPDATE OF s""", (before,))
            rows = cur.fetchall()
            for signal_id, strategy_id, status in rows:
                outcome_type = OUTCOME_TYPE_BY_STRATEGY.get(strategy_id)
                if outcome_type is None:
                    report["skipped_unsupported_strategy"].append(signal_id)
                    continue
                if status is None:
                    cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                                   (signal_id, outcome_type, status, realized_r, exit_timestamp, source)
                                   VALUES (%s, %s, 'INVALIDATED', NULL, %s, %s)
                                   ON CONFLICT (signal_id) DO NOTHING""", (signal_id, outcome_type, now, strategy_id))
                else:
                    cur.execute("""UPDATE strategy.entry_signal_outcomes
                                   SET status = 'INVALIDATED', realized_r = NULL, exit_timestamp = %s, updated_at = %s
                                   WHERE signal_id = %s AND status = 'OPEN'""", (now, now, signal_id))
                if cur.rowcount != 1:
                    continue          # became terminal concurrently: never overwrite
                cur.execute("""INSERT INTO strategy.entry_signal_outcome_override
                               (override_id, signal_id, previous_status, new_status, reason, operator, created_at)
                               VALUES (%s, %s, %s, 'INVALIDATED', %s, %s, %s)
                               ON CONFLICT (signal_id, new_status) DO NOTHING""",
                            (override_id(signal_id), signal_id, status, reason, operator, now))
                bucket = report["invalidated"].setdefault(strategy_id, {"was_open": 0, "was_missing": 0})
                bucket["was_open" if status == "OPEN" else "was_missing"] += 1
        report["managed_trades_closed"] = len(close_terminal_trades(conn, now_utc=now))
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=date.fromisoformat, default=None,
                        help="UTC date; signals decided before its 00:00 UTC (default: today)")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from postgres.config import PostgresConfig
    from postgres.db import connect
    day = args.before or datetime.now(timezone.utc).date()
    before = datetime.combine(day, time.min, tzinfo=timezone.utc)
    report = invalidate(lambda: connect(PostgresConfig.from_env()), before=before, reason=args.reason.strip(),
                        operator=args.operator.strip(), apply=args.apply)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
