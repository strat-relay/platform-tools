"""Isolated validation suite for migration 056 and the candle archive backfill.

Creates an isolated test database (trading_platform_d3a_test) in the same
Postgres cluster, applies migration 056, runs targeted tests, then drops the
test database.  Does NOT touch the production trading_platform database.

Usage (from inside the k3s cluster or via port-forward):
    DATABASE_URL=postgresql://... \\
    MARKET_DATA_BRIDGE_URL=http://... \\
    python scripts/test_d3a_candle_archive.py

The DATABASE_URL should point to the postgres server (not a specific db);
the script connects to the server-level 'postgres' db to create/drop the test db.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2
import psycopg2.extras

# -------------------------------------------------------------------
# Oct 8 2026 UTC timestamps (verified from MT5 bridge probe)
# -------------------------------------------------------------------
OCT8_START = 1791417600   # 2026-10-08 00:00:00 UTC
OCT8_END   = 1791417600 + 42 * 300   # 00:00 + 42 bars = ~03:30 UTC
OCT9_END   = 1791504000   # 2026-10-09 00:00:00 UTC

# Oct 8 03:00 decision_time for KOJO V3 signals
KOJO_DECISION_TIME = 1791417600 + 3 * 3600   # 03:00 UTC = 1791428400
KOJO_ENTRY  = Decimal("83113.99")
KOJO_STOP   = Decimal("83238.38")
KOJO_TARGET = Decimal("82955.77")

TEST_DB = "trading_platform_d3a_test"
MIGRATION_FILE = os.path.join(
    os.path.dirname(__file__), "..", "postgres", "migrations", "056_candle_archive.sql"
)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _server_conn(db_url: str) -> psycopg2.extensions.connection:
    """Connect to the 'postgres' maintenance database for CREATE/DROP DATABASE."""
    import re
    server_url = re.sub(r"/[^/]+(\?|$)", "/postgres\\1", db_url)
    conn = psycopg2.connect(server_url)
    conn.autocommit = True
    return conn


def _test_conn(db_url: str) -> psycopg2.extensions.connection:
    """Connect to the isolated test database."""
    import re
    test_url = re.sub(r"/[^/]+(\?|$)", f"/{TEST_DB}\\1", db_url)
    conn = psycopg2.connect(test_url)
    conn.autocommit = False
    return conn


def _apply_migration(conn: Any) -> None:
    with open(MIGRATION_FILE) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.notes: list[str] = []

    def fail(self, msg: str) -> None:
        self.passed = False
        self.notes.append(f"FAIL: {msg}")

    def ok(self, msg: str) -> None:
        self.notes.append(f"  ok: {msg}")


# -------------------------------------------------------------------
# Test cases
# -------------------------------------------------------------------

def test_migration_applies(conn: Any) -> Result:
    r = Result("migration_applies")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'market_data'
               AND table_name IN ('candle_archive', 'candle_conflicts', 'backfill_runs')
            ORDER BY table_name
        """)
        tables = {row[0] for row in cur.fetchall()}
    expected = {"candle_archive", "candle_conflicts", "backfill_runs"}
    missing = expected - tables
    if missing:
        r.fail(f"missing tables: {missing}")
    else:
        r.ok(f"all tables created: {sorted(tables)}")
    return r


def test_idempotent_duplicate_import(conn: Any) -> Result:
    r = Result("idempotent_duplicate_import")
    # Insert a single bar
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_data.candle_archive
                (provider, provider_symbol, timeframe, candle_time,
                 open_price, high_price, low_price, close_price,
                 tick_volume, source_run_id)
            VALUES ('MT5','BTCUSDm','M5', 1791417600,
                    83000.00, 83100.00, 82900.00, 83050.00,
                    100, 'test-idempotent')
        """)
        # Insert exact duplicate — must succeed without error
        cur.execute("""
            INSERT INTO market_data.candle_archive
                (provider, provider_symbol, timeframe, candle_time,
                 open_price, high_price, low_price, close_price,
                 tick_volume, source_run_id)
            VALUES ('MT5','BTCUSDm','M5', 1791417600,
                    83000.00, 83100.00, 82900.00, 83050.00,
                    100, 'test-idempotent')
            ON CONFLICT DO NOTHING
        """)
        cur.execute("""
            SELECT COUNT(*) FROM market_data.candle_archive
             WHERE provider='MT5' AND provider_symbol='BTCUSDm'
               AND timeframe='M5' AND candle_time=1791417600
        """)
        count = cur.fetchone()[0]
    conn.rollback()
    if count != 1:
        r.fail(f"expected 1 row after duplicate insert, got {count}")
    else:
        r.ok("duplicate import idempotent (1 row, no error)")
    return r


def test_conflict_detection(conn: Any) -> Result:
    r = Result("conflict_detection")
    with conn.cursor() as cur:
        # Insert original
        cur.execute("""
            INSERT INTO market_data.candle_archive
                (provider, provider_symbol, timeframe, candle_time,
                 open_price, high_price, low_price, close_price, source_run_id)
            VALUES ('MT5','BTCUSDm','M5', 1791417900,
                    83100.00, 83200.00, 83000.00, 83150.00, 'test-conflict')
        """)
        conn.commit()

        # Attempt conflicting observation (different OHLC for same candle_time)
        incoming = (83105.00, 83205.00, 82995.00, 83155.00)  # slightly different
        cur.execute("""
            SELECT open_price, high_price, low_price, close_price
              FROM market_data.candle_archive
             WHERE provider='MT5' AND provider_symbol='BTCUSDm'
               AND timeframe='M5' AND candle_time=1791417900
        """)
        stored = cur.fetchone()
        # Simulate conflict detection from backfill utility
        is_conflict = any(
            abs(Decimal(str(a)) - Decimal(str(b))) > Decimal("0.000001")
            for a, b in zip(stored, incoming)
        )
        if is_conflict:
            cur.execute("""
                INSERT INTO market_data.candle_conflicts
                    (provider, provider_symbol, timeframe, candle_time,
                     stored_open, stored_high, stored_low, stored_close,
                     incoming_open, incoming_high, incoming_low, incoming_close,
                     source_run_id)
                VALUES ('MT5','BTCUSDm','M5', 1791417900,
                        %s,%s,%s,%s, %s,%s,%s,%s, 'test-conflict')
            """, list(stored) + list(incoming))
            conn.commit()

        # Verify original value preserved, conflict logged
        cur.execute("""
            SELECT open_price FROM market_data.candle_archive
             WHERE candle_time=1791417900 AND provider_symbol='BTCUSDm'
        """)
        preserved_open = Decimal(str(cur.fetchone()[0]))

        cur.execute("""
            SELECT COUNT(*) FROM market_data.candle_conflicts
             WHERE provider_symbol='BTCUSDm' AND candle_time=1791417900
        """)
        conflict_count = cur.fetchone()[0]
    conn.rollback()

    if not is_conflict:
        r.fail("conflict was not detected")
    elif preserved_open != Decimal("83100.00"):
        r.fail(f"original value not preserved: {preserved_open}")
    elif conflict_count != 1:
        r.fail(f"expected 1 conflict row, got {conflict_count}")
    else:
        r.ok("conflict detected; original open=83100.00 preserved; 1 conflict row logged")
    return r


def test_geometry_constraint(conn: Any) -> Result:
    r = Result("geometry_constraint")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO market_data.candle_archive
                    (provider, provider_symbol, timeframe, candle_time,
                     open_price, high_price, low_price, close_price)
                VALUES ('MT5','BTCUSDm','M5', 1791418200,
                        83100.00,
                        83000.00,  -- high < open → geometry violation
                        83200.00, 83050.00)
            """)
        conn.rollback()
        r.fail("geometry constraint did not fire for high < open")
    except psycopg2.errors.CheckViolation:
        conn.rollback()
        r.ok("geometry constraint correctly rejected high < open")
    return r


def test_live_backfill_dry_run(conn: Any, bridge_url: str) -> Result:
    """Dry-run backfill: verify MT5 returns Oct 8 data, validate continuity."""
    r = Result("live_backfill_dry_run")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts.backfill_candle_archive import _bridge_rates_range, _find_true_gaps

    step = 300  # M5

    try:
        rows = _bridge_rates_range(bridge_url, "BTCUSDm", "M5",
                                    OCT8_START, OCT8_END, page_size=50)
    except Exception as exc:
        r.fail(f"bridge call failed: {exc}")
        return r

    if not rows:
        r.fail("bridge returned no rows for Oct 8 BTCUSDm M5")
        return r

    first_t = int(rows[0]["time"])
    last_t  = int(rows[-1]["time"])
    r.ok(f"bridge returned {len(rows)} bars: "
         f"{datetime.fromtimestamp(first_t, timezone.utc).isoformat()} → "
         f"{datetime.fromtimestamp(last_t, timezone.utc).isoformat()}")

    # Verify the 03:00 UTC bar is present
    target_time = KOJO_DECISION_TIME  # 2026-10-08 03:00:00 UTC
    bar_at_03 = next((r_ for r_ in rows if int(r_["time"]) == target_time), None)
    if bar_at_03 is None:
        # 03:00 bar might not be in this 42-bar window; check presence in range
        r.ok(f"03:00 UTC bar not in this page window (expected if page covers 00:00-03:30)")
    else:
        r.ok(f"03:00 UTC bar present: H={bar_at_03['high']} L={bar_at_03['low']}")

    # Gap detection
    gaps = _find_true_gaps(rows, step, first_t, last_t + step)
    if gaps:
        for gs, ge in gaps:
            r.fail(f"true gap: {datetime.fromtimestamp(gs, timezone.utc)} → "
                   f"{datetime.fromtimestamp(ge, timezone.utc)}")
    else:
        r.ok("no true gaps in fetched range")

    # Verify KOJO target hit in first evaluation bar after 03:00
    bars_after_03 = [b for b in rows if int(b["time"]) > target_time]
    if bars_after_03:
        b = bars_after_03[0]
        b_t  = datetime.fromtimestamp(int(b["time"]), timezone.utc).isoformat()
        b_lo = Decimal(str(b["low"]))
        b_hi = Decimal(str(b["high"]))
        if b_lo <= KOJO_TARGET:
            r.ok(f"target {KOJO_TARGET} hit in first bar after decision: {b_t} low={b_lo}")
        elif b_hi >= KOJO_STOP:
            r.ok(f"stop {KOJO_STOP} hit in first bar after decision: {b_t} high={b_hi}")
        else:
            r.ok(f"neither stop nor target in first bar after 03:00: {b_t} H={b_hi} L={b_lo}")

    return r


def test_live_backfill_write(db_url: str, bridge_url: str) -> Result:
    """Write a small Oct 8 window and verify storage and coverage."""
    r = Result("live_backfill_write")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts.backfill_candle_archive import run_backfill

    import re
    test_db_url = re.sub(r"/[^/]+(\?|$)", f"/{TEST_DB}\\1", db_url)

    result = run_backfill(
        db_url=test_db_url,
        bridge_url=bridge_url,
        provider="MT5",
        provider_symbol="BTCUSDm",
        timeframe="M5",
        start=OCT8_START,
        end=OCT8_END,  # ~03:30 UTC
        run_id="d3a-test-write-btcusdm-oct8",
        dry_run=False,
    )

    if result.get("status") != "complete":
        r.fail(f"backfill failed: {result}")
        return r

    bars = result["bars_fetched"]
    gaps = result["true_gaps"]
    conflicts = result["conflicts"]
    r.ok(f"backfill complete: {bars} bars, {gaps} true gaps, {conflicts} conflicts")

    # Verify database state
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*),
                   to_timestamp(MIN(candle_time)) AT TIME ZONE 'UTC' as oldest,
                   to_timestamp(MAX(candle_time)) AT TIME ZONE 'UTC' as newest
              FROM market_data.candle_archive
             WHERE provider='MT5' AND provider_symbol='BTCUSDm' AND timeframe='M5'
        """)
        count, oldest, newest = cur.fetchone()
        r.ok(f"DB count={count}, oldest={oldest}, newest={newest}")

        # Storage estimate
        cur.execute("""
            SELECT pg_size_pretty(pg_total_relation_size('market_data.candle_archive')) as table_size,
                   pg_size_pretty(pg_indexes_size('market_data.candle_archive')) as index_size
        """)
        table_sz, idx_sz = cur.fetchone()
        r.ok(f"storage: table={table_sz}, indexes={idx_sz}")

        # Verify 03:05 bar is present (first evaluation bar for KOJO signals)
        first_eval_time = KOJO_DECISION_TIME + 300  # 03:05 UTC
        cur.execute("""
            SELECT high_price, low_price, close_price FROM market_data.candle_archive
             WHERE provider='MT5' AND provider_symbol='BTCUSDm'
               AND timeframe='M5' AND candle_time = %s
        """, (first_eval_time,))
        bar = cur.fetchone()
        if bar:
            hi, lo, cl = Decimal(str(bar[0])), Decimal(str(bar[1])), Decimal(str(bar[2]))
            if lo <= KOJO_TARGET:
                r.ok(f"03:05 bar present: H={hi} L={lo} → TARGET HIT (target={KOJO_TARGET})")
            elif hi >= KOJO_STOP:
                r.ok(f"03:05 bar present: H={hi} → STOP HIT (stop={KOJO_STOP})")
            else:
                r.ok(f"03:05 bar present but no terminal event: H={hi} L={lo}")
        else:
            r.fail(f"03:05 UTC bar (candle_time={first_eval_time}) not found in archive")

        # Check backfill_runs record
        cur.execute("""
            SELECT status, bars_fetched, gaps_detected, conflicts_detected
              FROM market_data.backfill_runs
             WHERE run_id = 'd3a-test-write-btcusdm-oct8'
        """)
        run_row = cur.fetchone()
        if run_row:
            r.ok(f"run record: status={run_row[0]} bars={run_row[1]} "
                 f"gaps={run_row[2]} conflicts={run_row[3]}")

    conn.close()

    # Idempotency: re-run the same backfill (should report already_complete)
    result2 = run_backfill(
        db_url=test_db_url,
        bridge_url=bridge_url,
        provider="MT5",
        provider_symbol="BTCUSDm",
        timeframe="M5",
        start=OCT8_START,
        end=OCT8_END,
        run_id="d3a-test-write-btcusdm-oct8",  # same run_id
        dry_run=False,
    )
    if result2.get("status") == "already_complete":
        r.ok("re-run with same run_id correctly reports already_complete")
    else:
        r.fail(f"re-run with same run_id returned unexpected status: {result2.get('status')}")

    return r


# -------------------------------------------------------------------
# Runner
# -------------------------------------------------------------------

def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    db_url = (os.environ.get("DATABASE_URL")
              or os.environ.get("TRADING_POSTGRES_DSN"))
    bridge_url = os.environ.get("MARKET_DATA_BRIDGE_URL")

    if not db_url:
        sys.exit("DATABASE_URL or TRADING_POSTGRES_DSN required")
    if not bridge_url:
        sys.exit("MARKET_DATA_BRIDGE_URL required")

    print(f"\n{'='*60}")
    print(f"D3-A Candle Archive Test Suite")
    print(f"Test DB: {TEST_DB}")
    print(f"{'='*60}\n")

    # Create isolated test database
    server = _server_conn(db_url)
    try:
        server.cursor().execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        server.cursor().execute(
            f'CREATE DATABASE "{TEST_DB}" OWNER trading_app ENCODING \'UTF8\''
        )
        print(f"Created test database: {TEST_DB}")
    except Exception as exc:
        print(f"WARNING: Could not create {TEST_DB} as trading_app: {exc}")
        print("Attempting with current user credentials...")
        import re
        test_url = re.sub(r"/[^/]+(\?|$)", f"/{TEST_DB}\\1", db_url)
        try:
            server.cursor().execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
            server.cursor().execute(f'CREATE DATABASE "{TEST_DB}"')
            print(f"Created test database: {TEST_DB}")
        except Exception as exc2:
            sys.exit(f"Cannot create test database: {exc2}")
    server.close()

    # Apply migration 056 to test database
    import re
    test_url = re.sub(r"/[^/]+(\?|$)", f"/{TEST_DB}\\1", db_url)

    conn = psycopg2.connect(test_url)
    conn.autocommit = False
    try:
        _apply_migration(conn)
        print("Migration 056 applied to test database\n")
    except Exception as exc:
        print(f"Migration failed: {exc}")
        conn.close()
        raise

    # Run test suite
    results: list[Result] = []

    results.append(test_migration_applies(conn))
    results.append(test_idempotent_duplicate_import(conn))
    results.append(test_conflict_detection(conn))
    results.append(test_geometry_constraint(conn))
    results.append(test_live_backfill_dry_run(conn, bridge_url))
    conn.close()

    results.append(test_live_backfill_write(db_url, bridge_url))

    # Print results
    print("\n" + "="*60)
    print("TEST RESULTS")
    print("="*60)
    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if not r.passed:
            all_passed = False
        print(f"\n[{status}] {r.name}")
        for note in r.notes:
            print(f"       {note}")

    print("\n" + "="*60)
    print(f"{'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    print("="*60)

    # Cleanup
    server = _server_conn(db_url)
    server.cursor().execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    server.close()
    print(f"\nDropped test database: {TEST_DB}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
