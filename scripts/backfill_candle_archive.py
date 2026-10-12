"""Restartable historical candle backfill from the MT5 research bridge.

Usage:
    python scripts/backfill_candle_archive.py \\
        --symbol BTCUSDm --timeframe M5 \\
        --start 1791417600 --end 1791604800 \\
        --run-id backfill-btcusdm-20261012 \\
        --dry-run   # omit to write

The utility is restartable: if interrupted, re-run with the same --run-id and it
resumes from the last committed page checkpoint.  Conflicting observations (same
candle_time but different OHLC) are logged to market_data.candle_conflicts and
never silently overwrite the stored value.

Environment:
    DATABASE_URL or TRADING_POSTGRES_DSN  — PostgreSQL connection string
    MARKET_DATA_BRIDGE_URL               — MT5 bridge base URL (e.g. http://10.x.x.x:22347/mcp)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.request import Request, urlopen

try:
    import psycopg as _pg          # psycopg v3 (platform standard)
    _PG3 = True
except ImportError:
    import psycopg2 as _pg         # psycopg2 fallback (local dev / CI)
    _PG3 = False

log = logging.getLogger("backfill_candle_archive")

# Session-closure hours (UTC) known for MT5 broker feed.
# These gaps are expected and do not indicate missing data.
_MAINTENANCE_HOURS_UTC = frozenset({23})   # 23:00-23:45 UTC daily rollover
_TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
PAGE_BARS = 500


# ---------------------------------------------------------------------------
# Bridge call
# ---------------------------------------------------------------------------

def _bridge_rates_range(
    url: str,
    symbol: str,
    timeframe: str,
    start: int,
    end: int,
    page_size: int = PAGE_BARS,
) -> list[dict[str, Any]]:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": f"backfill-{time.time_ns()}",
        "method": "tools/call",
        "params": {"name": "mt5_rates_range", "arguments": {
            "symbol": symbol, "timeframe": timeframe,
            "start_timestamp": start, "end_timestamp": end,
            "page_size": page_size, "completed_only": True,
        }},
    }).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    resp = json.loads(urlopen(req, timeout=35).read())
    text = resp["result"]["content"][0]["text"]
    result = json.loads(text)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"Bridge error: {result['error']}")
    rows = result.get("rates", []) if isinstance(result, dict) else result
    return sorted(
        (r for r in rows if isinstance(r, dict) and "time" in r),
        key=lambda r: int(r["time"]),
    )


# ---------------------------------------------------------------------------
# Session-closure classification
# ---------------------------------------------------------------------------

def _is_session_closure(candle_time: int, step: int) -> bool:
    """True if the slot at candle_time is a weekend or daily rollover gap."""
    dt = datetime.fromtimestamp(candle_time, timezone.utc)
    if dt.weekday() in (5, 6):   # Saturday, Sunday
        return True
    if dt.hour in _MAINTENANCE_HOURS_UTC:
        return True
    return False


def _find_true_gaps(
    rows: list[dict[str, Any]],
    step: int,
    window_start: int,
    window_end: int,
) -> list[tuple[int, int]]:
    """Return (gap_start, gap_end) pairs for unexpected missing slots in [window_start, window_end)."""
    present = {int(r["time"]) for r in rows}
    gaps: list[tuple[int, int]] = []
    t = window_start
    while t < window_end:
        if t not in present and not _is_session_closure(t, step):
            # Find extent of this gap
            gap_s = t
            while t < window_end and t not in present and not _is_session_closure(t, step):
                t += step
            gaps.append((gap_s, t))
        else:
            t += step
    return gaps


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _upsert_bars(
    cur: Any,
    provider: str,
    provider_symbol: str,
    timeframe: str,
    rows: list[dict[str, Any]],
    run_id: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Insert rows, detect conflicts.  Returns (inserted, conflicts)."""
    if not rows or dry_run:
        return (0, 0)

    inserted = 0
    conflicts = 0

    for row in rows:
        ct = int(row["time"])
        op, hi, lo, cl = (
            _decimal(row["open"]), _decimal(row["high"]),
            _decimal(row["low"]), _decimal(row["close"]),
        )
        tv = row.get("tick_volume")
        rv = row.get("real_volume")
        sp = row.get("spread")

        # Try insert; if conflict exists, check for OHLC mismatch
        cur.execute("""
            INSERT INTO market_data.candle_archive
                (provider, provider_symbol, timeframe, candle_time,
                 open_price, high_price, low_price, close_price,
                 tick_volume, real_volume, spread, source_run_id)
            VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s)
            ON CONFLICT (provider, provider_symbol, timeframe, candle_time) DO NOTHING
        """, (provider, provider_symbol, timeframe, ct,
              op, hi, lo, cl, tv, rv, sp, run_id))

        if cur.rowcount == 0:
            # Candle already in archive — check for conflict
            cur.execute("""
                SELECT open_price, high_price, low_price, close_price
                  FROM market_data.candle_archive
                 WHERE provider=%s AND provider_symbol=%s AND timeframe=%s AND candle_time=%s
            """, (provider, provider_symbol, timeframe, ct))
            stored = cur.fetchone()
            if stored is not None:
                s_op, s_hi, s_lo, s_cl = [_decimal(v) for v in stored]
                if any(abs(a - b) > Decimal("0.000001")
                       for a, b in [(s_op, op), (s_hi, hi), (s_lo, lo), (s_cl, cl)]):
                    cur.execute("""
                        INSERT INTO market_data.candle_conflicts
                            (provider, provider_symbol, timeframe, candle_time,
                             stored_open, stored_high, stored_low, stored_close,
                             incoming_open, incoming_high, incoming_low, incoming_close,
                             source_run_id)
                        VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s)
                    """, (provider, provider_symbol, timeframe, ct,
                          s_op, s_hi, s_lo, s_cl,
                          op, hi, lo, cl, run_id))
                    conflicts += 1
                    log.warning(json.dumps({
                        "event": "candle_conflict_detected",
                        "provider_symbol": provider_symbol,
                        "candle_time": ct,
                        "human_time": datetime.fromtimestamp(ct, timezone.utc).isoformat(),
                        "stored": {"open": str(s_op), "high": str(s_hi),
                                   "low": str(s_lo), "close": str(s_cl)},
                        "incoming": {"open": str(op), "high": str(hi),
                                     "low": str(lo), "close": str(cl)},
                    }))
        else:
            inserted += 1

    return inserted, conflicts


def _decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise ValueError(f"Cannot convert to Decimal: {v!r}")


def _ensure_run(conn: Any, run_id: str, provider: str, provider_symbol: str,
                timeframe: str, start: int, end: int) -> int | None:
    """Return checkpoint_cursor for an existing run, or create a new one."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, checkpoint_cursor, bars_fetched
              FROM market_data.backfill_runs
             WHERE run_id = %s
        """, (run_id,))
        row = cur.fetchone()
    if row:
        status, cursor, fetched = row
        if status == "COMPLETE":
            log.info(json.dumps({"event": "run_already_complete", "run_id": run_id,
                                  "bars_fetched": fetched}))
            return None  # signal caller to exit
        log.info(json.dumps({"event": "resuming_run", "run_id": run_id,
                               "status": status, "checkpoint_cursor": cursor}))
        return cursor
    # New run
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_data.backfill_runs
                (run_id, provider, provider_symbol, timeframe,
                 requested_start, requested_end, checkpoint_cursor, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'IN_PROGRESS')
        """, (run_id, provider, provider_symbol, timeframe, start, end, start))
    conn.commit()
    return start


def _advance_checkpoint(conn: Any, run_id: str, cursor: int,
                         bars_fetched: int, gaps: int, conflicts: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE market_data.backfill_runs
               SET checkpoint_cursor  = %s,
                   bars_fetched       = bars_fetched + %s,
                   gaps_detected      = gaps_detected + %s,
                   conflicts_detected = conflicts_detected + %s
             WHERE run_id = %s
        """, (cursor, bars_fetched, gaps, conflicts, run_id))
    conn.commit()


def _complete_run(conn: Any, run_id: str, status: str = "COMPLETE",
                  error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE market_data.backfill_runs
               SET status       = %s,
                   completed_at = now(),
                   last_error   = %s
             WHERE run_id = %s
        """, (status, error, run_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------

def run_backfill(
    *,
    db_url: str,
    bridge_url: str,
    provider: str,
    provider_symbol: str,
    timeframe: str,
    start: int,
    end: int,
    run_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    step = _TIMEFRAME_SECONDS.get(timeframe)
    if step is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if start >= end:
        raise ValueError(f"start ({start}) must be < end ({end})")

    conn = _pg.connect(db_url, autocommit=False) if _PG3 else _pg.connect(db_url)
    if not _PG3:
        conn.autocommit = False

    try:
        cursor = _ensure_run(conn, run_id, provider, provider_symbol,
                              timeframe, start, end)
    except Exception as _e:
        if "UndefinedTable" in type(_e).__name__ or "does not exist" in str(_e):
            conn.rollback()
            raise RuntimeError(
                "market_data schema not found — apply migration 056 before running backfill"
            )
        raise

    if cursor is None:  # already complete
        conn.close()
        return {"status": "already_complete", "run_id": run_id}

    total_fetched = 0
    total_gaps: list[tuple[int, int]] = []
    total_conflicts = 0
    page_start = cursor if cursor > start else start
    page_size_seconds = PAGE_BARS * step

    log.info(json.dumps({
        "event": "backfill_started",
        "run_id": run_id,
        "provider_symbol": provider_symbol,
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "resume_from": page_start,
        "dry_run": dry_run,
        "human_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "human_end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
    }))

    while page_start < end:
        page_end = min(end, page_start + page_size_seconds)

        try:
            rows = _bridge_rates_range(
                bridge_url, provider_symbol, timeframe,
                page_start, page_end,
            )
        except Exception as exc:
            log.error(json.dumps({
                "event": "bridge_page_failed",
                "run_id": run_id, "page_start": page_start, "page_end": page_end,
                "error": str(exc)[:300],
            }))
            _complete_run(conn, run_id, "FAILED", str(exc)[:500])
            conn.close()
            return {"status": "failed", "error": str(exc), "page_start": page_start}

        # Session-aware gap detection on this page
        page_gaps = _find_true_gaps(rows, step, page_start, page_end)
        if page_gaps:
            for gs, ge in page_gaps:
                log.error(json.dumps({
                    "event": "true_bar_gap",
                    "run_id": run_id,
                    "provider_symbol": provider_symbol,
                    "gap_start": gs,
                    "gap_end": ge,
                    "human_start": datetime.fromtimestamp(gs, timezone.utc).isoformat(),
                    "human_end": datetime.fromtimestamp(ge, timezone.utc).isoformat(),
                }))

        # Log page summary
        log.info(json.dumps({
            "event": "page_fetched",
            "run_id": run_id,
            "page_start": page_start,
            "page_end": page_end,
            "bars_returned": len(rows),
            "true_gaps": len(page_gaps),
            "dry_run": dry_run,
            "human_page_start": datetime.fromtimestamp(page_start, timezone.utc).isoformat(),
        }))

        with conn.cursor() as cur:
            inserted, conflicts = _upsert_bars(
                cur, provider, provider_symbol, timeframe,
                rows, run_id, dry_run,
            )

        if not dry_run:
            _advance_checkpoint(conn, run_id, page_end,
                                  len(rows), len(page_gaps), conflicts)

        total_fetched += len(rows)
        total_gaps.extend(page_gaps)
        total_conflicts += conflicts
        page_start = page_end

    if not dry_run:
        _complete_run(conn, run_id)

    conn.close()

    summary = {
        "status": "complete",
        "run_id": run_id,
        "dry_run": dry_run,
        "provider_symbol": provider_symbol,
        "timeframe": timeframe,
        "bars_fetched": total_fetched,
        "true_gaps": len(total_gaps),
        "conflicts": total_conflicts,
        "gap_details": [(datetime.fromtimestamp(gs, timezone.utc).isoformat(),
                         datetime.fromtimestamp(ge, timezone.utc).isoformat())
                        for gs, ge in total_gaps],
    }
    log.info(json.dumps({"event": "backfill_complete", **summary}))
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol",    required=True, help="Provider symbol (e.g. BTCUSDm)")
    parser.add_argument("--timeframe", required=True, help="Timeframe (M5, M15, H1, H4)")
    parser.add_argument("--start",     required=True, type=int, help="Start Unix timestamp (inclusive)")
    parser.add_argument("--end",       required=True, type=int, help="End Unix timestamp (exclusive)")
    parser.add_argument("--run-id",    required=True, dest="run_id", help="Unique run identifier")
    parser.add_argument("--provider",  default="MT5", help="Provider name (default: MT5)")
    parser.add_argument("--dry-run",   action="store_true", dest="dry_run",
                        help="Fetch and validate but do not write to database")
    parser.add_argument("--db-url",    default=None, dest="db_url",
                        help="PostgreSQL DSN (default: DATABASE_URL or TRADING_POSTGRES_DSN env)")
    parser.add_argument("--bridge-url", default=None, dest="bridge_url",
                        help="Bridge base URL (default: MARKET_DATA_BRIDGE_URL env)")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL") or os.environ.get("TRADING_POSTGRES_DSN")
    if not db_url:
        sys.exit("DATABASE_URL or TRADING_POSTGRES_DSN must be set")
    bridge_url = args.bridge_url or os.environ.get("MARKET_DATA_BRIDGE_URL")
    if not bridge_url:
        sys.exit("MARKET_DATA_BRIDGE_URL must be set")

    result = run_backfill(
        db_url=db_url,
        bridge_url=bridge_url,
        provider=args.provider,
        provider_symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        run_id=args.run_id,
        dry_run=args.dry_run,
    )
    sys.exit(0 if result.get("status") in ("complete", "already_complete") else 1)


if __name__ == "__main__":
    main()
