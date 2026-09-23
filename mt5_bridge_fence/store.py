"""Durable, crash-surviving persistence for the real bridge fence boundary. SQLite (stdlib
`sqlite3`, no new dependency) in WAL mode with `synchronous=FULL`: every commit is fsync'd before
the call returns, so fence-generation state and the idempotency ledger are both actually on disk
- not an in-memory dict - by the time `advance_fence()`/`submit()` return (mission
`CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION` section 5/6: "do not rely solely on PostgreSQL
lookup at request time"; "do not use in-memory dictionaries as authoritative protection").

This is the bridge's OWN, independent store - a separate file from the platform's PostgreSQL,
modelling the real deployment (the bridge process has its own local disk, not access to the
platform database). `RealBridgeFenceBoundary.restart()` closes and reopens this same file to
prove state actually survives a process boundary, not just a Python-level flag reset.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class FenceStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        # check_same_thread=False: the real deployment constructs this store and calls
        # serve_forever() in the same thread (no issue at all); the test/proof harness
        # constructs it in the main thread and runs the (deliberately single-threaded, see
        # http_server.py) server loop in one background thread so a test can act as a
        # concurrent HTTP client - access to this connection is always strictly serialized to
        # that one server thread either way, never touched concurrently from two threads at
        # once, which is exactly the safe use of check_same_thread=False.
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)  # autocommit; we manage transactions explicitly
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")  # fsync on every commit - durability over throughput
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""CREATE TABLE IF NOT EXISTS fence_state (
            resource text PRIMARY KEY,
            generation integer NOT NULL,
            holder text,
            grant_expires_at text,
            advanced_at text
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS idempotency_ledger (
            attempt_id text PRIMARY KEY,
            resource text NOT NULL,
            state text NOT NULL,
            broker_response_json text,
            created_at text NOT NULL,
            updated_at text NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idempotency_ledger_resource_idx ON idempotency_ledger(resource)")
        conn.execute("""CREATE TABLE IF NOT EXISTS bridge_meta (
            key text PRIMARY KEY,
            value text NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO bridge_meta (key, value) VALUES ('bridge_epoch', '0')")
        return conn

    def close(self) -> None:
        self._conn.close()

    def reopen(self) -> None:
        """Simulates a real process restart at the storage layer: close the connection and
        reopen the SAME on-disk file, so whatever is returned afterward is provably read from
        disk, not from any in-memory structure that happened to survive."""
        self._conn.close()
        self._conn = self._open()

    # -- fence_state ----------------------------------------------------------------------

    def get_fence_state(self, resource: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT resource, generation, holder, grant_expires_at, advanced_at FROM fence_state WHERE resource=?",
            (resource,)).fetchone()
        if row is None:
            return None
        return {"resource": row[0], "generation": row[1], "holder": row[2],
               "grant_expires_at": row[3], "advanced_at": row[4]}

    def upsert_fence_state(self, *, resource: str, generation: int, holder: str,
                           grant_expires_at: str, advanced_at: str) -> None:
        self._conn.execute("""INSERT INTO fence_state (resource, generation, holder, grant_expires_at, advanced_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(resource) DO UPDATE SET generation=excluded.generation, holder=excluded.holder,
                grant_expires_at=excluded.grant_expires_at, advanced_at=excluded.advanced_at""",
                          (resource, generation, holder, grant_expires_at, advanced_at))

    # -- idempotency_ledger -----------------------------------------------------------------

    def get_ledger_entry(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT attempt_id, resource, state, broker_response_json FROM idempotency_ledger WHERE attempt_id=?",
            (attempt_id,)).fetchone()
        if row is None:
            return None
        return {"attempt_id": row[0], "resource": row[1], "state": row[2],
               "broker_response": json.loads(row[3]) if row[3] is not None else None}

    def upsert_ledger_entry(self, *, attempt_id: str, resource: str, state: str,
                            broker_response: dict[str, Any] | None, now: str) -> None:
        payload = json.dumps(broker_response) if broker_response is not None else None
        self._conn.execute("""INSERT INTO idempotency_ledger
                (attempt_id, resource, state, broker_response_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state,
                broker_response_json=excluded.broker_response_json, updated_at=excluded.updated_at""",
                          (attempt_id, resource, state, payload, now, now))

    def ledger_entries_for_resource(self, resource: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT attempt_id, resource, state, broker_response_json FROM idempotency_ledger WHERE resource=?",
            (resource,)).fetchall()
        return [{"attempt_id": r[0], "resource": r[1], "state": r[2],
                "broker_response": json.loads(r[3]) if r[3] is not None else None} for r in rows]

    def sweep_in_progress_to_uncertain(self) -> list[str]:
        """Called on every boundary restart (mission section 6/7): any ledger row still in
        SUBMISSION_IN_PROGRESS - meaning `submit()` durably recorded "about to call the broker"
        but the process ended before the durable DISPATCHED write with the broker's actual
        response - becomes UNCERTAIN_AFTER_RESTART. A row that already reached DISPATCHED is
        deliberately left alone: that write only happens with the broker's own response already
        in hand and already fsync'd, so it is a settled fact, not something a restart should
        cast doubt on (an improvement the real boundary's two-phase write makes possible - the
        in-memory-only test simulator cannot distinguish these two cases at all and treats every
        prior success as equally uncertain after a restart)."""
        rows = self._conn.execute("SELECT attempt_id FROM idempotency_ledger WHERE state='SUBMISSION_IN_PROGRESS'").fetchall()
        attempt_ids = [r[0] for r in rows]
        if attempt_ids:
            self._conn.executemany(
                "UPDATE idempotency_ledger SET state='UNCERTAIN_AFTER_RESTART' WHERE attempt_id=?",
                [(a,) for a in attempt_ids])
        return attempt_ids

    def cancel_pending_for_resource(self, resource: str, *, exempt_states: tuple[str, ...]) -> list[str]:
        """On a generation advance past the current one (a new owner taking over), any ledger
        entry for THIS resource not already in a terminal/settled state is cancelled - scoped by
        resource (unlike the test-only simulator's global sweep, which is a documented modelling
        simplification this real implementation does not need to repeat)."""
        placeholders = ",".join("?" for _ in exempt_states)
        rows = self._conn.execute(
            f"SELECT attempt_id FROM idempotency_ledger WHERE resource=? AND state NOT IN ({placeholders})",
            (resource, *exempt_states)).fetchall()
        attempt_ids = [r[0] for r in rows]
        if attempt_ids:
            self._conn.executemany(
                "UPDATE idempotency_ledger SET state='CANCELLED_FENCED' WHERE attempt_id=?",
                [(a,) for a in attempt_ids])
        return attempt_ids

    # -- bridge_meta ------------------------------------------------------------------------

    def get_bridge_epoch(self) -> int:
        row = self._conn.execute("SELECT value FROM bridge_meta WHERE key='bridge_epoch'").fetchone()
        return int(row[0]) if row else 0

    def increment_bridge_epoch(self) -> int:
        epoch = self.get_bridge_epoch() + 1
        self._conn.execute("UPDATE bridge_meta SET value=? WHERE key='bridge_epoch'", (str(epoch),))
        return epoch
