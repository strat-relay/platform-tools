"""In-process fakes for execution_v2 unit tests only: NOT a database, NOT a broker. Matches the
exact SQL surface `intent.py`/`worker.py`/`reconcile.py` issue, including `platform.
acquire_ownership`/`platform.assert_generation` (real PL/pgSQL functions this fake simulates in
Python so pure unit tests do not need a live PostgreSQL - the REAL functions are proven against
real PostgreSQL separately, see docs/v2_execution/README.md and test_execution_v2_real_postgres.py).

`FakeBroker` is the simulated MT5 boundary itself (mission section 13): a plain callable
returning canned responses (FILLED / REJECTED / AMBIGUOUS-response-lost), used as the
`broker_call` the bridge fence simulator invokes only after every independent check has passed.
It never touches a network socket, port 22347, or port 22348.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


class StaleFencingGeneration(RuntimeError):
    pass


_ENTRY_SIGNAL_COLUMNS = ("signal_id", "strategy_id", "strategy_version", "strategy_ref", "instrument",
                        "direction", "decision_time", "entry_price", "stop_price", "target_price",
                        "entry_signal_hash")

_INTENT_COLUMNS = ("execution_intent_id", "entry_signal_id", "entry_signal_hash", "strategy_id",
                  "strategy_version", "strategy_ref", "instrument", "direction", "order_type",
                  "requested_entry_price", "stop_price", "target_price", "approved_volume",
                  "risk_fraction", "risk_policy_version", "account_id", "broker", "idempotency_key",
                  "status", "block_reason")

_RESULT_COLUMNS = ("execution_result_id", "attempt_id", "execution_intent_id", "outcome", "account_id",
                   "broker_order_id", "broker_deal_id", "symbol", "volume", "requested_price",
                   "actual_price", "submitted_at", "confirmed_at", "raw_broker_evidence")


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

        if "PLATFORM.ACQUIRE_OWNERSHIP" in upper:
            self._result = (self.conn.acquire_ownership(*params),)
            return
        if "PLATFORM.ASSERT_GENERATION" in upper:
            self.conn.assert_generation(*params)
            return
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

    def _select(self, upper: str, sql: str, params: Any) -> None:
        if "STRATEGY.ENTRY_SIGNALS" in upper:
            row = self.conn.entry_signals.get(params[0])
            self._result = tuple(row[k] for k in _ENTRY_SIGNAL_COLUMNS) if row else None
            return
        if "PLATFORM.OWNERSHIP_LEASES" in upper:
            self._result = (self.conn.leases.get(params[0], {}).get("generation", 0),) if params[0] in self.conn.leases else None
            return
        if "EXECUTION_V2.EXECUTION_ATTEMPT" in upper and "ATTEMPT_ID, STATE" in upper:
            row = next((r for r in self.conn.view("execution_v2.execution_attempt").values()
                       if r["execution_intent_id"] == params[0]), None)
            self._result = (row["attempt_id"], row["state"], row["generation"], row["account_id"]) if row else None
            return
        if "EXECUTION_V2.EXECUTION_INTENT" in upper:
            row = self.conn.view("execution_v2.execution_intent").get(params[0])
            self._result = (tuple(row[k] for k in ("instrument", "direction", "approved_volume", "stop_price",
                                                    "target_price", "requested_entry_price"))
                           if row else None)
            return
        if "EXECUTION_V2.EXECUTION_RESULT" in upper:
            row = next((r for r in self.conn.view("execution_v2.execution_result").values()
                       if r["attempt_id"] == params[0]), None)
            self._result = (row["outcome"],) if row else None
            return
        raise AssertionError(f"FakeCursor cannot SELECT: {sql[:100]}")

    def _insert(self, upper: str, sql: str, params: Any) -> None:
        do_nothing = "DO NOTHING" in upper
        table, key, row = self.conn.build_insert(upper, sql, params)
        existing_rows = self.conn.pending_and_committed(table)
        # ON CONFLICT can target a UNIQUE column other than the table's own primary key (e.g.
        # execution_intent's real conflict target is entry_signal_id, not its PK
        # execution_intent_id) - conflict-check on that column specifically rather than assuming
        # the storage key IS the conflict target, so direct PK lookups (SELECT ... WHERE
        # execution_intent_id=%s, as worker.py does) still work against the same table.
        conflict_field = _ON_CONFLICT_FIELD.get(table)
        if conflict_field:
            existing = next((r for r in existing_rows.values() if r.get(conflict_field) == row.get(conflict_field)), None)
        else:
            existing = existing_rows.get(key)
        if existing is not None:
            if not do_nothing:
                raise AssertionError(f"unexpected PK/UNIQUE collision on {table}: {key}")
            self.rowcount = 0
            self._result = None
            return
        self.conn.pending[table][key] = row
        self.rowcount = 1
        self._result = (key,) if "RETURNING" in upper else None

    def _update(self, upper: str, sql: str, params: Any) -> None:
        if "EXECUTION_V2.EXECUTION_ATTEMPT" in upper:
            # WHERE attempt_id=%s, but the table is stored keyed by its own PRIMARY KEY
            # (execution_intent_id, since execution_attempt has a real UNIQUE(execution_intent_id)
            # conflict target) - scan by the attempt_id *field*, matching how _load_attempt already
            # resolves rows, rather than treating attempt_id as the dict key.
            attempt_id = params[-1]
            table = self.conn.pending_and_committed("execution_v2.execution_attempt")
            storage_key = next((k for k, r in table.items() if r["attempt_id"] == attempt_id), None)
            if storage_key is None:
                self.rowcount = 0
                return
            updated = dict(table[storage_key])
            updated["state"] = params[0]
            self.conn.pending["execution_v2.execution_attempt"][storage_key] = updated
            self.rowcount = 1
            return
        raise AssertionError(f"FakeCursor cannot UPDATE: {sql[:100]}")

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> list[Any]:
        return []


_ON_CONFLICT_FIELD = {
    # execution_intent's real ON CONFLICT target (migration 016) is the UNIQUE entry_signal_id
    # column, not its execution_intent_id primary key.
    "execution_v2.execution_intent": "entry_signal_id",
}


_INSERT_TABLE_MARKERS = (
    ("PLATFORM.OUTBOX_EVENTS", "platform.outbox_events"),
    ("EXECUTION_V2.RECONCILIATION_FINDING", "execution_v2.reconciliation_finding"),
    ("EXECUTION_V2.EXECUTION_INTENT", "execution_v2.execution_intent"),
    ("EXECUTION_V2.EXECUTION_ATTEMPT", "execution_v2.execution_attempt"),
    ("EXECUTION_V2.EXECUTION_RESULT", "execution_v2.execution_result"),
)


@dataclass
class FakeConnection:
    tables: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    pending: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    entry_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        self.entry_signals[fields["signal_id"]] = {k: fields.get(k) for k in _ENTRY_SIGNAL_COLUMNS}

    # -- platform.ownership_leases / acquire_ownership / assert_generation simulation ----------

    def acquire_ownership(self, lease_key: str, holder_instance_id: str, expected_generation: int) -> int:
        """Mirrors postgres/migrations/008_stratrelay_foundation.sql's real CAS semantics."""
        lease = self.leases.get(lease_key)
        if lease is None:
            if expected_generation not in (0, None):
                # No row exists yet; any expected_generation is accepted for a brand-new key,
                # matching the real function's behaviour (EXISTS check only fires for existing rows).
                pass
            self.leases[lease_key] = {"holder_instance_id": holder_instance_id, "generation": 1}
            return 1
        if lease["generation"] != expected_generation:
            raise StaleFencingGeneration(f"STALE_FENCING_GENERATION for {lease_key}")
        lease["generation"] += 1
        lease["holder_instance_id"] = holder_instance_id
        return lease["generation"]

    def assert_generation(self, lease_key: str, expected_generation: int) -> None:
        lease = self.leases.get(lease_key)
        if lease is None or lease["generation"] != expected_generation:
            raise StaleFencingGeneration(f"STALE_FENCING_GENERATION for {lease_key}")

    def build_insert(self, upper: str, sql: str, params: Any) -> tuple[str, Any, dict[str, Any]]:
        table = next((name for marker, name in _INSERT_TABLE_MARKERS if marker in upper), None)
        if table is None:
            raise AssertionError(f"unrecognized INSERT target: {sql[:100]}")
        builder = getattr(self, f"_row_{table.replace('.', '_')}")
        return (table, *builder(params))

    def _row_platform_outbox_events(self, params: Any) -> tuple[Any, dict[str, Any]]:
        (event_id, event_type, aggregate_type, aggregate_id, aggregate_version, schema_version,
         payload, occurred_at, correlation_id, causation_id) = params
        return event_id, {"event_id": event_id, "event_type": event_type, "aggregate_type": aggregate_type,
                          "aggregate_id": aggregate_id, "payload": payload, "occurred_at": occurred_at,
                          "correlation_id": correlation_id, "causation_id": causation_id,
                          "publish_status": "RECEIVED"}

    def _row_execution_v2_reconciliation_finding(self, params: Any) -> tuple[Any, dict[str, Any]]:
        keys = ("finding_id", "attempt_id", "broker_truth", "broker_order_id", "detail")
        if len(params) == 4:  # the QUARANTINED path (intent.py) omits broker_order_id
            keys = ("finding_id", "attempt_id", "broker_truth", "detail")
        row = dict(zip(keys, params))
        row.setdefault("broker_order_id", None)
        return row["finding_id"], row

    def _row_execution_v2_execution_intent(self, params: Any) -> tuple[Any, dict[str, Any]]:
        # intent.py's real INSERT hardcodes order_type='MARKET' and broker='MT5' as SQL literals
        # (never bound params) - the params tuple therefore has 18 entries against 20 columns.
        # Zip against the column list with those two columns removed, then fill the literals back
        # in, instead of naively zipping the full 20-column tuple (which silently drops the last
        # two columns, `status`/`block_reason`, off the end).
        bound_columns = tuple(c for c in _INTENT_COLUMNS if c not in ("order_type", "broker"))
        row = dict(zip(bound_columns, params))
        row["order_type"] = "MARKET"
        row["broker"] = "MT5"
        return row["execution_intent_id"], row  # keyed by the real PRIMARY KEY; entry_signal_id
                                                # uniqueness is enforced separately (see _ON_CONFLICT_FIELD)
                                                # so PK lookups (worker.py's _load_intent) resolve correctly.

    def _row_execution_v2_execution_attempt(self, params: Any) -> tuple[Any, dict[str, Any]]:
        # worker.py's real INSERT hardcodes state='CLAIMED' as a SQL literal (never a bound
        # param) - 5 params for 6 columns, same pattern as execution_intent's order_type/broker.
        keys = ("attempt_id", "execution_intent_id", "account_id", "resource", "generation")
        row = dict(zip(keys, params))
        row["state"] = "CLAIMED"
        return row["execution_intent_id"], row  # ON CONFLICT (execution_intent_id)

    def _row_execution_v2_execution_result(self, params: Any) -> tuple[Any, dict[str, Any]]:
        row = dict(zip(_RESULT_COLUMNS, params))
        return row["attempt_id"], row  # ON CONFLICT (attempt_id)


@dataclass
class FakeBroker:
    """The simulated MT5 execution boundary (mission section 13). `mode` controls what the next
    call returns; call count is tracked so tests can assert a duplicate delivery never triggers
    a second broker call."""

    mode: str = "fill"  # fill | reject | ambiguous
    calls: int = 0
    broker_order_seq: int = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.mode == "reject":
            return {"status": "REJECTED", "reason": "SIMULATED_BROKER_REJECT"}
        if self.mode == "ambiguous":
            return {"status": "AMBIGUOUS", "reason": "SIMULATED_RESPONSE_LOST"}
        self.broker_order_seq += 1
        return {"status": "FILLED", "broker_order_id": f"SIMORD-{self.broker_order_seq}",
               "broker_deal_id": f"SIMDEAL-{self.broker_order_seq}", "actual_price": None}
