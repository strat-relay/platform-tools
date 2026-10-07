"""One-time seed: load the existing context_structure_retrace_post_exit.jsonl into PostgreSQL.

Usage:
    TRADING_POSTGRES_DSN="..." python postgres/seed_context_post_exit_observations.py \
        [--ledger /path/to/context_structure_retrace_post_exit.jsonl]

The script is idempotent — it uses ON CONFLICT DO NOTHING so re-running after a partial
load picks up from where it left off.  Progress is printed every 1 000 rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_LEDGER = Path("/work/context-research/context_structure_retrace_post_exit.jsonl")

INSERT_SQL = """
INSERT INTO research.context_post_exit_observations
       (record_hash, signal_id, trade_id, record_type, observed_at, payload)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (record_hash) DO NOTHING
"""

BATCH = 500


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Install psycopg (psycopg3) to run this script") from exc
    return psycopg.connect(dsn, autocommit=False)


def seed(ledger_path: Path, dsn: str) -> None:
    if not ledger_path.exists():
        raise SystemExit(f"Ledger file not found: {ledger_path}")

    total = inserted = skipped = errors = 0
    conn = _connect(dsn)
    batch: list[tuple] = []

    def flush():
        nonlocal inserted
        if not batch:
            return
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, batch)
            inserted += cur.rowcount if cur.rowcount >= 0 else 0
        conn.commit()
        batch.clear()

    with ledger_path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            total += 1
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                errors += 1
                continue

            record_hash = record.get("record_hash", "")
            if not record_hash:
                skipped += 1
                continue

            batch.append((
                record_hash,
                record.get("signal_id"),
                record.get("trade_id"),
                record.get("record_type", ""),
                record.get("observed_at"),
                json.dumps(record, sort_keys=True, default=str),
            ))

            if len(batch) >= BATCH:
                flush()

            if total % 1000 == 0:
                print(f"  {total:,} lines read, {inserted:,} inserted so far…", flush=True)

    flush()
    conn.close()
    print(f"Done. {total:,} lines, {inserted:,} inserted, {skipped:,} skipped (no hash), {errors:,} parse errors.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()
    dsn = os.environ.get("TRADING_POSTGRES_DSN", "")
    if not dsn:
        raise SystemExit("Set TRADING_POSTGRES_DSN before running this script")
    seed(args.ledger, dsn)


if __name__ == "__main__":
    main()
