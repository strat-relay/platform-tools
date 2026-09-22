"""In-memory fakes for tests and the local benchmark: NOT a database or message broker.

No live PostgreSQL or NATS is reachable in this environment (verified before writing this
module) and none is deployed by this branch. `FakeSignalDatabase` is a dict-backed connection
matching the exact SQL surface `migration.signal.ingest_signal`/`postgres.foundation.persist_evaluation`
/`claim_inbox`/`mark_inbox_processed` issue (verified by reading those modules), with optional
injected per-statement latency so the benchmark can model realistic transaction/contention costs
without ever asserting these ARE production PostgreSQL numbers (see
docs/nats_first_data_plane/04_BENCHMARK_RESULTS.md). `FailingJetStream` extends the existing
`infrastructure.messaging.testing.InMemoryJetStream` harness with configurable failure/latency
injection for the JetStream-unavailable and restart/recovery scenarios.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from infrastructure.messaging.testing import InMemoryJetStream, PendingMessage

_TABLE_RE = re.compile(r"(?:INSERT INTO|UPDATE|FROM)\s+([a-zA-Z0-9_.]+)", re.IGNORECASE)

# Table -> which positional bind parameter is its logical primary key, for this fake's
# purposes only (not a general SQL engine). Matches the exact statements in
# migration/signal.py, postgres/foundation.py at this commit.
_PK_INDEX = {
    "strategy.candidates": 0,               # candidate_id
    "strategy.evaluations": 0,              # evaluation_id
    "strategy.decision_traces": 0,          # evaluation_id
    "strategy.entry_signals": None,         # named params; PK = signal_id
    "strategy.signals": 0,                  # signal_id
    "strategy.entry_signal_mechanisms": None,  # composite (entry_signal_id, mechanism); ON CONFLICT DO NOTHING
    "platform.outbox_events": 0,            # event_id
    "platform.reason_codes": None,          # composite (code, version)
    "strategy.evaluation_reason_codes": None,
    "strategy.stage_results": None,
    "platform.inbox_events": None,          # composite (consumer_name, event_id)
}


@dataclass
class FakeCursor:
    conn: "FakeConnection"
    rowcount: int = 0
    _last_result: Any = None
    _last_rows: list[Any] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, args: Any = None) -> None:
        if self.conn.latency_fn is not None:
            self.conn.latency_fn()
        self.conn.statement_count += 1
        table_match = _TABLE_RE.search(sql)
        table = table_match.group(1) if table_match else None
        upper = sql.strip().upper()
        params = args if isinstance(args, dict) else (args or ())

        if table == "strategy.entry_signals" and upper.startswith("SELECT"):
            signal_id = params[0] if not isinstance(params, dict) else params.get("signal_id")
            row = self.conn.view(table).get(signal_id)
            self._last_result = (row["entry_signal_hash"],) if row else None
            self.rowcount = 1 if row else 0
            return

        if table == "strategy.entry_signal_mechanisms" and upper.startswith("SELECT"):
            entry_signal_id = params[0] if not isinstance(params, dict) else params.get("entry_signal_id")
            rows = [row for key, row in self.conn.view(table).items() if key[0] == entry_signal_id]
            rows.sort(key=lambda row: row["position"])
            self._last_rows = [(row["mechanism"],) for row in rows]
            return

        if upper.startswith("INSERT"):
            self._insert(table, sql, params)
            return
        if upper.startswith("UPDATE"):
            self._update(table, sql, params)
            return
        raise AssertionError(f"FakeCursor cannot handle statement: {sql[:80]}")

    def _insert(self, table: str | None, sql: str, params: Any) -> None:
        if table is None:
            raise AssertionError(f"unrecognized INSERT target: {sql[:80]}")
        on_conflict_do_nothing = "DO NOTHING" in sql.upper()
        key = self.conn.record_key(table, sql, params)
        existing = self.conn.pending_and_committed(table).get(key)
        if existing is not None and on_conflict_do_nothing:
            self.rowcount = 0
            return
        row = self.conn.build_row(table, sql, params)
        self.conn.pending[table][key] = row
        self.rowcount = 1

    def _update(self, table: str | None, sql: str, params: Any) -> None:
        if table == "platform.outbox_events":
            event_id = params[-1]
            row = self.conn.pending_and_committed(table).get(event_id)
            if row is not None:
                updated = dict(row)
                if "publish_status='PUBLISHED'" in sql:
                    updated["publish_status"] = "PUBLISHED"
                elif "publish_status='FAILED'" in sql:
                    updated["publish_status"] = "FAILED"
                self.conn.pending[table][event_id] = updated
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if table == "platform.inbox_events":
            consumer_name, event_id = params
            key = (consumer_name, event_id)
            row = self.conn.pending_and_committed(table).get(key)
            if row is not None:
                updated = dict(row); updated["status"] = "PROCESSED"
                self.conn.pending[table][key] = updated
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        raise AssertionError(f"FakeCursor cannot UPDATE table: {table}")

    def fetchone(self) -> Any:
        return self._last_result

    def fetchall(self) -> list[Any]:
        return self._last_rows or []


@dataclass
class FakeConnection:
    """Dict-backed fake PostgreSQL connection: real commit/rollback staging semantics, no
    network, no server. `latency_fn` (if set) is called once per `execute()`, synchronously -
    matching this codebase's own synchronous psycopg usage inside async callers
    (infrastructure/messaging/outbox_relay.py does the same)."""
    tables: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    pending: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    latency_fn: Callable[[], None] | None = None
    statement_count: int = 0
    commits: int = 0
    rollbacks: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        for table, rows in self.pending.items():
            self.tables[table].update(rows)
        self.pending = defaultdict(dict)
        self.commits += 1

    def rollback(self) -> None:
        self.pending = defaultdict(dict)
        self.rollbacks += 1

    def view(self, table: str) -> dict[Any, dict[str, Any]]:
        return self.pending_and_committed(table)

    def pending_and_committed(self, table: str) -> dict[Any, dict[str, Any]]:
        return {**self.tables.get(table, {}), **self.pending.get(table, {})}

    def record_key(self, table: str, sql: str, params: Any) -> Any:
        if table == "strategy.entry_signals":
            return params["signal_id"] if isinstance(params, dict) else params[0]
        if table == "strategy.entry_signal_mechanisms":
            return (params[0], params[1])
        if table == "platform.reason_codes":
            return (params[0], params[1])
        if table == "strategy.evaluation_reason_codes":
            return (params[0], params[1])
        if table == "strategy.stage_results":
            return (params[0], params[1])
        if table == "platform.inbox_events":
            return (params[0], params[1])
        index = _PK_INDEX.get(table)
        if index is None:
            raise AssertionError(f"no primary-key rule for table: {table}")
        return params[index]

    def build_row(self, table: str, sql: str, params: Any) -> dict[str, Any]:
        if table == "strategy.entry_signals":
            return dict(params)
        if table == "strategy.entry_signal_mechanisms":
            return {"entry_signal_id": params[0], "mechanism": params[1], "position": params[2]}
        if table == "platform.outbox_events":
            return {"event_id": params[0], "event_type": params[1], "aggregate_type": params[2],
                    "aggregate_id": params[3], "payload": params[4], "publish_status": "RECEIVED"}
        if table == "platform.inbox_events":
            return {"consumer_name": params[0], "event_id": params[1], "status": "RECEIVED"}
        return {"_params": params}

    def signal_row(self, signal_id: str) -> dict[str, Any] | None:
        return self.pending_and_committed("strategy.entry_signals").get(signal_id)

    def outbox_rows(self) -> list[dict[str, Any]]:
        return list(self.pending_and_committed("platform.outbox_events").values())


class FailingJetStream(InMemoryJetStream):
    """InMemoryJetStream plus injectable publish failure/latency, for the JetStream-outage and
    restart/recovery scenarios (dataplane/benchmark.py, tests/test_dataplane_failure_scenarios.py)."""

    def __init__(self, *, latency_fn: Callable[[], None] | None = None):
        super().__init__()
        self.latency_fn = latency_fn
        self.available = True
        self.publish_calls = 0

    async def publish(self, subject: str, payload: bytes, **kwargs: Any) -> dict[str, Any]:
        self.publish_calls += 1
        if self.latency_fn is not None:
            self.latency_fn()
        if not self.available:
            raise ConnectionError("JetStream unavailable (simulated)")
        return await super().publish(subject, payload, **kwargs)
