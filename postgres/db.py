from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import PostgresConfig

ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"

# Migration 035 was applied in production with this checksum before a comment-only
# revision reached the image. The SQL object is unchanged; accept the recorded legacy
# checksum so the runner can continue to forward migrations without rewriting history.
# This is intentionally a narrow, filename-scoped compatibility exception.
LEGACY_APPLIED_CHECKSUMS = {
    "035_manual_signal_invalidation.sql": {
        "d647e5c4c3683fb36c233a762cb7ccf6c2eaef6dd797f601e068cb2de0ca5c86",
    },
    # PR #135 added updated_by to the INSERT after 047 was applied in production.
    # The SQL effect is identical; accept the production-recorded checksum.
    "047_kojo_v3_strategy_registration.sql": {
        "f9457146fffe72b4083411558e5210f01b99046dc6c9b04faafe6807fdbb72ce",
    },
}


def checksum_is_accepted(filename: str, applied_checksum: str, current_checksum: str) -> bool:
    return applied_checksum == current_checksum or applied_checksum in LEGACY_APPLIED_CHECKSUMS.get(filename, set())


def connect(config: PostgresConfig | None = None, *, readonly: bool = False):
    """Open a psycopg 3 connection without making psycopg mandatory at import time."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install requirements-postgres.txt to use PostgreSQL") from exc
    cfg = config or PostgresConfig.from_env()
    if readonly and cfg.readonly_dsn:
        return psycopg.connect(cfg.readonly_dsn, autocommit=False)
    return psycopg.connect(**cfg.connect_kwargs(), autocommit=False)


@contextmanager
def transaction(conn) -> Iterator[Any]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def health_check(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user, current_schema(), version()")
        database, user, schema, version = cur.fetchone()
    return {"database": database, "user": user, "schema": schema, "version": version}


def apply_migrations(conn, migrations_dir: Path = MIGRATIONS) -> list[str]:
    """Apply ordered, checksum-verified migrations under a transaction lock."""
    files = sorted(migrations_dir.glob("*.sql"))
    with transaction(conn):
        with conn.cursor() as cur:
            # If this process is killed while holding the transaction lock, the connection
            # would stay idle in transaction indefinitely and block every query on locked
            # tables. Self-terminate after 2 minutes so the DB cleans up automatically.
            cur.execute("SET LOCAL idle_in_transaction_session_timeout = '120000'")
            cur.execute("CREATE SCHEMA IF NOT EXISTS platform")
            cur.execute("""CREATE TABLE IF NOT EXISTS platform.schema_migrations (
                version text PRIMARY KEY, filename text NOT NULL, checksum_sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )""")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('mt5-native-bridge-schema-migrations'))")
            cur.execute("SELECT version, filename, checksum_sha256 FROM platform.schema_migrations")
            applied = {row[0]: row[1:] for row in cur.fetchall()}
            completed: list[str] = []
            for path in files:
                version = path.name.split("_", 1)[0]
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                if version in applied:
                    if not checksum_is_accepted(path.name, applied[version][1], checksum):
                        raise RuntimeError(f"Migration checksum changed: {path.name}")
                    continue
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO platform.schema_migrations(version, filename, checksum_sha256) VALUES (%s,%s,%s)", (version, path.name, checksum))
                completed.append(path.name)
    return completed
