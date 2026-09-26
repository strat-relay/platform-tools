"""In-process fakes for tests only: NOT a database. No live PostgreSQL is reachable in this
sandbox (no psycopg installed, no localhost:5432). `FakeConnection`/`FakeCursor` match the
exact SQL surface this package's modules issue (`managed_trade.py`, `observation.py`,
`tm_none.py`, `binding.py`) plus the shared `postgres.foundation` inbox helpers, with real
commit/rollback staging and `SELECT ... FOR UPDATE` row-locking-order semantics good enough for
single-threaded tests. `strategy.entry_signals` rows are seeded directly by tests (this package
never inserts into that P2-owned table), via `FakeConnection.seed_entry_signal(...)`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeCursor:
    conn: "FakeConnection"
    rowcount: int = 0
    _result: Any = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.statement_count += 1
        upper = " ".join(sql.split()).upper()
        params = params or ()
        self._result = None
        self.rowcount = 0

        if upper.startswith("SELECT"):
            self._select(upper, sql, params)
            return
        if upper.startswith("INSERT"):
            self._insert(upper, sql, params)
            return
        if upper.startswith("UPDATE"):
            self._update(upper, sql, params)
            return
        raise AssertionError(f"FakeCursor cannot handle statement: {sql[:100]}")

    # -- SELECT ------------------------------------------------------------------------

    def _select(self, upper: str, sql: str, params: Any) -> None:
        if "STRATEGY.ENTRY_SIGNALS" in upper:
            row = self.conn.view("strategy.entry_signals").get(params[0])
            self._result = tuple(row[k] for k in _ENTRY_SIGNAL_COLUMNS) if row else None
            return
        if "TRADE_MANAGEMENT.TRADE_MANAGER_VERSION" in upper and "EVALUATOR_ID, MANIFEST" in upper:
            row = self.conn.view("trade_management.trade_manager_version").get(params[0])
            self._result = (row["evaluator_id"], row["manifest"]) if row else None
            return
        if "TRADE_MANAGEMENT.TRADE_MANAGER_VERSION" in upper:
            row = self.conn.view("trade_management.trade_manager_version").get(params[0])
            self._result = (row["status"],) if row else None
            return
        if "TRADE_MANAGEMENT.LEGACY_STREAM_BINDING" in upper:
            strategy_id, decision_time, instance_id, instrument = params
            rows = [r for r in self.conn.view("trade_management.legacy_stream_binding").values()
                    if r["strategy_id"] == strategy_id and str(r["valid_from"]) <= str(decision_time)
                    and (r["strategy_instance_id"] is None or r["strategy_instance_id"] == instance_id)
                    and (r["instrument"] is None or r["instrument"] == instrument)]
            rows.sort(key=lambda r: str(r["valid_from"]), reverse=True)
            self._rows = [(r["binding_id"], r["tm_version_id"], r["binding_hash"],
                          r["strategy_instance_id"], r["instrument"]) for r in rows]
            self._result = "MULTI"
            return
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper and "ENTRY_SIGNAL_ID=" in upper.replace(" ", ""):
            row = next((r for r in self.conn.view("trade_management.managed_trade").values()
                       if r["entry_signal_id"] == params[0]), None)
            self._result = (row["entry_signal_hash"],) if row else None
            return
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper and "REFERENCE_ENTRY_PRICE" in upper:
            # Checked before the "STATE FROM" branch below: this query's own column list ends
            # "...RISK_DISTANCE, STATE FROM TRADE_MANAGEMENT.MANAGED_TRADE...", so "STATE FROM"
            # is a substring of it too - order matters here, not just presence.
            row = self.conn.view("trade_management.managed_trade").get(params[0])
            if row is None:
                self._result = None
            else:
                self._result = (row["direction"], row["reference_entry_price"], row["initial_stop"],
                                row["risk_distance"], row["state"])
            return
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper and "STATE FROM" in upper:
            row = self.conn.view("trade_management.managed_trade").get(params[0])
            self._result = (row["state"],) if row else None
            return
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper:
            row = self.conn.view("trade_management.managed_trade").get(params[0])
            if row is None:
                self._result = None
            else:
                self._result = (row["managed_trade_id"], row["state"], row["tm_version_id"],
                                row["last_observation_seq"], row["instrument"])
            return
        if "TRADE_MANAGEMENT.TRADE_OBSERVATION" in upper and "OBSERVATION_SEQ FROM" in upper:
            row = self.conn.view("trade_management.trade_observation").get(params[0])
            self._result = (row["observation_seq"],) if row else None
            return
        if "TRADE_MANAGEMENT.TRADE_OBSERVATION" in upper and "MARKET_SNAPSHOT_ID" in upper:
            row = self.conn.view("trade_management.trade_observation").get(params[0])
            if row is None:
                self._result = None
            else:
                self._result = (row["observation_id"], row["managed_trade_id"], row["observation_seq"],
                                row["tm_version_id"], row["market_snapshot_id"], row["effective_at"],
                                row["data_status"])
            return
        if "TRADE_MANAGEMENT.TRADE_OBSERVATION" in upper:
            row = self.conn.view("trade_management.trade_observation").get(params[0])
            if row is None:
                self._result = None
            else:
                self._result = (row["observation_id"], row["managed_trade_id"], row["observation_seq"],
                                row["tm_version_id"], row["effective_at"], row["data_status"])
            return
        if "TRADE_MANAGEMENT.TRADE_MANAGER_DECISION" in upper and "WHERE MANAGED_TRADE_ID" in upper:
            managed_trade_id = params[0]
            rows = [r for r in self.conn.view("trade_management.trade_manager_decision").values()
                   if r["managed_trade_id"] == managed_trade_id]
            if not rows:
                self._result = None
            else:
                latest = max(rows, key=lambda r: r["observation_seq"])
                self._result = (latest["action"], latest["parameters"])
            return
        if "TRADE_MANAGEMENT.TRADE_MANAGER_DECISION" in upper:
            row = self.conn.view("trade_management.trade_manager_decision").get(params[0])
            self._result = (row["action"],) if row else None
            return
        if "TRADE_MANAGEMENT.MARKET_SNAPSHOT" in upper:
            row = self.conn.view("trade_management.market_snapshot").get(params[0])
            self._result = (row["bid"], row["ask"]) if row else None
            return
        raise AssertionError(f"FakeCursor cannot SELECT: {sql[:100]}")

    def fetchone(self) -> Any:
        return None if self._result in (None, "MULTI") else self._result

    def fetchall(self) -> list[Any]:
        return getattr(self, "_rows", [])

    # -- INSERT ------------------------------------------------------------------------

    def _insert(self, upper: str, sql: str, params: Any) -> None:
        do_nothing = "DO NOTHING" in upper
        table, key, row = self.conn.build_insert(upper, sql, params)
        existing = self.conn.pending_and_committed(table).get(key)
        if existing is not None:
            if not do_nothing:
                raise AssertionError(f"unexpected primary-key collision on {table}: {key}")
            self.rowcount = 0
            self._result = None
            return
        self.conn.pending[table][key] = row
        self.rowcount = 1
        self._result = (key,) if "RETURNING" in upper else None

    # -- UPDATE ------------------------------------------------------------------------

    def _update(self, upper: str, sql: str, params: Any) -> None:
        if "PLATFORM.INBOX_EVENTS" in upper:
            consumer_name, event_id = params
            key = (consumer_name, event_id)
            row = self.conn.pending_and_committed("platform.inbox_events").get(key)
            if row is None:
                self.rowcount = 0
                return
            updated = dict(row)
            updated["status"] = "PROCESSED"
            self.conn.pending["platform.inbox_events"][key] = updated
            self.rowcount = 1
            return
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper and "LAST_OBSERVATION_SEQ" in upper:
            seq, managed_trade_id = params
            row = self.conn.pending_and_committed("trade_management.managed_trade").get(managed_trade_id)
            if row is None:
                self.rowcount = 0
                return
            updated = dict(row)
            updated["last_observation_seq"] = seq
            self.conn.pending["trade_management.managed_trade"][managed_trade_id] = updated
            self.rowcount = 1
            return
        raise AssertionError(f"FakeCursor cannot UPDATE: {sql[:100]}")


_ENTRY_SIGNAL_COLUMNS = ("signal_id", "strategy_id", "strategy_version", "strategy_ref",
                         "parameter_set_ref", "parameter_set_status", "strategy_instance_id",
                         "instrument", "direction", "decision_time", "entry_price", "stop_price",
                         "risk_distance", "target_price", "entry_signal_hash")

_INSERT_TABLE_MARKERS = (
    ("PLATFORM.INBOX_EVENTS", "platform.inbox_events"),
    ("PLATFORM.OUTBOX_EVENTS", "platform.outbox_events"),
    ("TRADE_MANAGEMENT.MANAGED_TRADE_QUARANTINE", "trade_management.managed_trade_quarantine"),
    ("TRADE_MANAGEMENT.MANAGED_TRADE_SKIP", "trade_management.managed_trade_skip"),
    ("TRADE_MANAGEMENT.MANAGED_TRADE", "trade_management.managed_trade"),
    ("TRADE_MANAGEMENT.TRADE_MANAGER_VERSION", "trade_management.trade_manager_version"),
    ("TRADE_MANAGEMENT.LEGACY_STREAM_BINDING", "trade_management.legacy_stream_binding"),
    ("TRADE_MANAGEMENT.MARKET_SNAPSHOT", "trade_management.market_snapshot"),
    ("TRADE_MANAGEMENT.TRADE_OBSERVATION", "trade_management.trade_observation"),
    ("TRADE_MANAGEMENT.TRADE_MANAGER_DECISION", "trade_management.trade_manager_decision"),
    ("TRADE_MANAGEMENT.PUBLICATION_DECISION", "trade_management.publication_decision"),
)


@dataclass
class FakeConnection:
    tables: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    pending: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
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

    def seed_entry_signal(self, **fields: Any) -> None:
        row = {k: fields.get(k) for k in _ENTRY_SIGNAL_COLUMNS}
        self.tables["strategy.entry_signals"][row["signal_id"]] = row

    def seed_tm_version(self, *, tm_version_id: str, evaluator_id: str = "tm-none.v1",
                        label: str = "TM-NONE-1", manifest_hash: str = "", status: str = "FROZEN",
                        manifest: dict[str, Any] | None = None) -> None:
        self.tables["trade_management.trade_manager_version"][tm_version_id] = {
            "tm_version_id": tm_version_id, "evaluator_id": evaluator_id, "label": label,
            "manifest_hash": manifest_hash, "status": status, "manifest": manifest or {},
        }

    def seed_legacy_binding(self, *, binding_id: str, strategy_id: str, tm_version_id: str,
                            valid_from: str, binding_hash: str, strategy_instance_id: str | None = None,
                            instrument: str | None = None) -> None:
        self.tables["trade_management.legacy_stream_binding"][binding_id] = {
            "binding_id": binding_id, "strategy_id": strategy_id, "strategy_instance_id": strategy_instance_id,
            "instrument": instrument, "tm_version_id": tm_version_id, "valid_from": valid_from,
            "binding_hash": binding_hash,
        }

    def build_insert(self, upper: str, sql: str, params: Any) -> tuple[str, Any, dict[str, Any]]:
        table = next((name for marker, name in _INSERT_TABLE_MARKERS if marker in upper), None)
        if table is None:
            raise AssertionError(f"unrecognized INSERT target: {sql[:100]}")
        builder = getattr(self, f"_row_{table.replace('.', '_')}")
        return (table, *builder(params))

    def _row_platform_inbox_events(self, params: Any) -> tuple[Any, dict[str, Any]]:
        consumer_name, event_id = params
        key = (consumer_name, event_id)
        return key, {"consumer_name": consumer_name, "event_id": event_id, "status": "RECEIVED"}

    def _row_platform_outbox_events(self, params: Any) -> tuple[Any, dict[str, Any]]:
        (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, schema_version,
         payload, occurred_at, correlation_id, causation_id) = params
        return event_id, {"event_id": event_id, "event_type": event_type, "aggregate_type": aggregate_type,
                          "aggregate_id": aggregate_id, "aggregate_version": aggregate_version,
                          "payload": payload, "occurred_at": occurred_at, "correlation_id": correlation_id,
                          "causation_id": causation_id, "publish_status": "RECEIVED"}

    def _row_trade_management_managed_trade_quarantine(self, params: Any) -> tuple[Any, dict[str, Any]]:
        entry_signal_id, expected_hash, actual_hash, reason = params
        return entry_signal_id, {"entry_signal_id": entry_signal_id, "expected_entry_signal_hash": expected_hash,
                                 "actual_entry_signal_hash": actual_hash, "reason": reason}

    def _row_trade_management_managed_trade_skip(self, params: Any) -> tuple[Any, dict[str, Any]]:
        entry_signal_id, reason, detail = params
        return entry_signal_id, {"entry_signal_id": entry_signal_id, "reason": reason, "detail": detail}

    def _row_trade_management_managed_trade(self, params: Any) -> tuple[Any, dict[str, Any]]:
        keys = ("managed_trade_id", "entry_signal_id", "entry_signal_hash", "strategy_id", "strategy_version",
                "strategy_ref", "parameter_set_ref", "parameter_set_status", "instrument", "direction",
                "decision_time", "reference_entry_price", "initial_stop", "initial_target", "risk_distance",
                "tm_version_id", "tm_binding_id", "binding_hash", "tm_bound_at", "binding_resolution",
                "evidence_mode", "eligibility", "eligibility_reason", "creation_lag_seconds",
                "record_mode", "state")
        row = dict(zip(keys, params))
        row["last_observation_seq"] = 0
        return row["managed_trade_id"], row

    def _row_trade_management_trade_manager_version(self, params: Any) -> tuple[Any, dict[str, Any]]:
        raise AssertionError("trade_manager_version is seeded via seed_tm_version(), never INSERTed by this package")

    def _row_trade_management_legacy_stream_binding(self, params: Any) -> tuple[Any, dict[str, Any]]:
        binding_id, strategy_id, strategy_instance_id, instrument, tm_version_id, valid_from, binding_hash = params
        row = {"binding_id": binding_id, "strategy_id": strategy_id, "strategy_instance_id": strategy_instance_id,
               "instrument": instrument, "tm_version_id": tm_version_id, "valid_from": valid_from,
               "binding_hash": binding_hash}
        return binding_id, row

    def _row_trade_management_market_snapshot(self, params: Any) -> tuple[Any, dict[str, Any]]:
        keys = ("market_snapshot_id", "provider_id", "feed_id", "instrument", "source_timestamp",
                "bid", "ask", "spread", "data_status", "quote_hash")
        row = dict(zip(keys, params))
        return row["market_snapshot_id"], row

    def _row_trade_management_trade_observation(self, params: Any) -> tuple[Any, dict[str, Any]]:
        keys = ("observation_id", "managed_trade_id", "observation_seq", "market_snapshot_id",
                "tm_version_id", "observed_at", "effective_at", "bars_ref", "data_status", "payload_hash")
        row = dict(zip(keys, params))
        return row["observation_id"], row

    def _row_trade_management_trade_manager_decision(self, params: Any) -> tuple[Any, dict[str, Any]]:
        keys = ("decision_id", "managed_trade_id", "tm_version_id", "observation_id", "observation_seq",
                "action", "parameters", "reason_codes", "decision_trace_ref", "decision_time",
                "persisted_at", "data_status", "record_mode")
        row = dict(zip(keys, params))
        return row["decision_id"], row

    def _row_trade_management_publication_decision(self, params: Any) -> tuple[Any, dict[str, Any]]:
        decision_id, outcome, reason = params
        return decision_id, {"decision_id": decision_id, "outcome": outcome, "reason": reason}
