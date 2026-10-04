"""Read-only platform-owned Control API routes.

This module intentionally has no filesystem runtime-state fallback.  Current
authority comes from the deployed authority environment, canonical PostgreSQL,
and (for the active strategy definition only) the mounted platform config.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict
from .instrument_membership import (DEFAULT_INSTANCE_BY_STRATEGY, InstrumentMembershipRepository,
                                     MembershipConflict, UnsupportedInstrument)
from .strategy_catalog import MembershipRefused

SCHEMA_VERSION = "012"
TRADE_MANAGEMENT_SCHEMA_VERSION = "013"
BROKER_POSITION_SCHEMA_VERSION = "022"
EXECUTION_RUNTIME_COMPONENT = "execution_v2"
LIMIT = 100

READ_ONLY_BROKER_TOOLS = {
    "account": ("mt5_account_info", None),
    "positions": ("mt5_positions", None),
    "pending-orders": ("mt5_orders", None),
    "history-orders": ("mt5_history", {"limit": 500}),
    "deals": ("mt5_history", {"limit": 500}),
    "symbols": ("mt5_symbols", None),
}


class ReadOnlyBridgeReader:
    """Allow-listed read-only client for the MT5 bridge."""

    def __init__(self, endpoint: str | Callable[[], str | None] | None, timeout: float = 20.0):
        # A callable endpoint is resolved per call (e.g. from platform.runtime_setting), so API
        # startup never depends on the database.
        self._endpoint = endpoint
        self.timeout = timeout

    @property
    def endpoint(self) -> str | None:
        value = self._endpoint() if callable(self._endpoint) else self._endpoint
        return value or None

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        if tool not in {name for name, _ in READ_ONLY_BROKER_TOOLS.values()}:
            raise ValueError("Control API permits only read-only broker tools")
        endpoint = self.endpoint
        if not endpoint:
            raise RuntimeError("Read-only MT5 bridge endpoint is not configured")
        payload = {"jsonrpc": "2.0", "id": "platform-control-api",
                   "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments or {}}}
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Bridge-Origin": "CONTROL_API"},
            method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            outer = json.loads(response.read())
        result = outer.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise RuntimeError(content[0].get("text", "read-only broker read failed"))
        content = result.get("content") or [{}]
        return json.loads(content[0].get("text", "null"))


class TtlCache:
    """Per-key, thread-safe, single-flight TTL cache for expensive read models: concurrent callers
    of an expired key wait for one computation instead of each running it. Failures are not cached."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl_seconds, clock
        self._values: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str, compute: Callable[[], Any]) -> Any:
        hit = self._values.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._values.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
            value = compute()
            self._values[key] = (self.clock(), value)
            return value


# Exact count(*) over the event and Trade Manager history tables (hundreds of thousands of rows)
# took seconds per request and queued every other request behind it. These status numbers are
# informational, so they come from PostgreSQL's maintained row estimates instead.
_ESTIMATE_SQL = ("SELECT greatest(reltuples, 0)::bigint FROM pg_class "
                 "WHERE oid = to_regclass(%s)")
TM_SUMMARY_TTL_SECONDS = float(os.getenv("TM_SUMMARY_CACHE_SECONDS", "60"))
_TM_SUMMARY_CACHE = TtlCache(TM_SUMMARY_TTL_SECONDS)


class PlatformControlRepository:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 012 is required")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL platform source unavailable") from exc

    def platform_status(self, *, include_event_counts: bool = True) -> dict[str, Any]:
        if not include_event_counts:
            rows = self.query("""SELECT
                0 AS outbox_count,
                0 AS inbox_count,
                (SELECT count(*) FROM platform.runtime_instances
                 WHERE component = 'orchestrator' AND status = 'RUNNING') AS orchestrator_running""")
            return rows[0]
        rows = self.query(f"""SELECT
            ({_ESTIMATE_SQL.replace('%s', "'platform.outbox_events'")}) AS outbox_count,
            ({_ESTIMATE_SQL.replace('%s', "'platform.inbox_events'")}) AS inbox_count,
            (SELECT count(*) FROM platform.runtime_instances WHERE component = 'orchestrator' AND status = 'RUNNING') AS orchestrator_running""")
        return {**rows[0], "event_counts_estimated": True}

    def execution_runtime_status(self) -> dict[str, Any]:
        """Read the V2 runtime's effective, persisted status projection."""
        rows = self.query("""SELECT instance_id, status, last_heartbeat_at, metadata
            FROM platform.runtime_instances
            WHERE component = %s
            ORDER BY last_heartbeat_at DESC, instance_id ASC
            LIMIT 1""", (EXECUTION_RUNTIME_COMPONENT,))
        if not rows:
            raise CanonicalSourceUnavailable("V2 execution runtime status is unavailable")
        runtime = rows[0]
        metadata = runtime.get("metadata") or {}
        policy = metadata.get("risk_policy") or {}
        canary_environment = metadata.get("canary_environment") or metadata.get("bridge_mode") or "real"
        canary_account_id = metadata.get("canary_account_id") or metadata.get("account_id")
        canary = None
        if canary_account_id:
            canary_rows = self.query("""SELECT canary_key, generation, lifecycle_state,
                    max_new_executions, consumed
                FROM execution_v2.canary_state
                WHERE environment = %s AND account_id = %s AND lifecycle_state = 'ACTIVE'
                ORDER BY generation DESC""", (canary_environment, canary_account_id))
            if len(canary_rows) > 1:
                raise CanonicalSourceUnavailable("multiple active canary windows")
            if canary_rows:
                canary = canary_rows[0]
        max_new = int((canary or {}).get("max_new_executions")
                      or policy.get("canary_max_new_executions") or 0)
        consumed = int((canary or {}).get("consumed") or 0)
        worker_status = "HEALTHY" if runtime.get("status") == "RUNNING" else "DOWN"
        if runtime.get("last_heartbeat_at") is None:
            worker_status = "DEGRADED"
        return {
            "instance_id": runtime.get("instance_id"),
            # ExecutionAuthorityApi._preflight uses the persisted runtime
            # lifecycle state to gate an authority transition. Keep the
            # canonical status alongside the operator-facing worker_status;
            # collapsing this to HEALTHY made the injected Arm path fail
            # while the direct DB preflight passed.
            "status": runtime.get("status"),
            "worker_status": worker_status,
            "execution_authority_mode": metadata.get("execution_authority_mode", "UNKNOWN"),
            "account_id": metadata.get("account_id"),
            "risk_policy": policy,
            # Canary counters remain visible for historical/operator context, but
            # normal V2 execution does not require an active window or capacity.
            "canary": {"key": (canary or {}).get("canary_key"),
                       "generation": (canary or {}).get("generation"),
                       "state": (canary or {}).get("lifecycle_state", "NONE"),
                       "max_new_executions": max_new, "consumed": consumed,
                       "remaining": max(0, max_new - consumed), "required": False},
            "execution_bridge": metadata.get("execution_bridge") or {"status": "UNKNOWN"},
            "broker_account": metadata.get("broker_account") or {"status": "UNKNOWN"},
        }

    def events(self, limit: int, offset: int, event_id: str | None = None) -> list[dict[str, Any]]:
        if event_id is not None:
            return self.query("""SELECT event_id, event_type, aggregate_type, aggregate_id,
                schema_version, payload, occurred_at, correlation_id, causation_id
                FROM platform.outbox_events WHERE event_id = %s""", (event_id,))
        return self.query("""SELECT event_id, event_type, aggregate_type, aggregate_id,
            schema_version, payload, occurred_at, correlation_id, causation_id
            FROM platform.outbox_events ORDER BY occurred_at DESC, event_id LIMIT %s OFFSET %s""", (limit, offset))

    def executions(self, limit: int, offset: int, intent_id: str | None = None) -> list[dict[str, Any]]:
        if intent_id is not None:
            return self.query("""SELECT i.intent_id, i.idempotency_key, i.intent_type, i.aggregate_id,
                i.status, i.payload, i.created_at, i.completed_at,
                r.result_id, r.broker_operation_id, r.status AS result_status, r.payload AS result_payload, r.recorded_at
                FROM execution.intents i LEFT JOIN execution.results r USING (intent_id)
                WHERE i.intent_id = %s ORDER BY r.recorded_at""", (intent_id,))
        return self.query("""SELECT i.intent_id, i.idempotency_key, i.intent_type, i.aggregate_id,
            i.status, i.payload, i.created_at, i.completed_at
            FROM execution.intents i ORDER BY i.created_at DESC, i.intent_id LIMIT %s OFFSET %s""", (limit, offset))

    def execution_metrics(self) -> dict[str, int]:
        rows = self.query("""SELECT
            (SELECT count(*) FROM execution.intents) AS execution_intents,
            (SELECT count(*) FROM execution.intents) AS broker_capable_requests_attempted,
            (SELECT count(*) FROM execution.results) AS mt5_order_send_attempted,
            (SELECT count(*) FROM execution.results WHERE status IN ('ACCEPTED','FILLED')) AS broker_orders_accepted,
            (SELECT count(*) FROM execution.results WHERE status = 'FILLED') AS broker_fills_observed,
            (SELECT count(*) FROM execution.results WHERE status = 'REJECTED') AS rejected,
            (SELECT count(*) FROM execution.results WHERE status = 'BLOCKED') AS blocked""")
        return rows[0]

    def _trade_management_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (TRADE_MANAGEMENT_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 013 is required")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL trade management source unavailable") from exc

    def trade_manager_summary(self) -> dict[str, Any]:
        """Computed at most once per TM_SUMMARY_CACHE_SECONDS (default 60) per process; the scans
        behind it (max() and filtered counts over the Trade Manager history tables) are costly."""
        return _TM_SUMMARY_CACHE.get("trade_manager_summary", self._trade_manager_summary)

    def _trade_manager_summary(self) -> dict[str, Any]:
        rows = self._trade_management_query("""SELECT
            (SELECT count(*) FROM trade_management.managed_trade) AS total_managed_trades,
            (SELECT count(*) FROM trade_management.managed_trade WHERE state = 'OPEN') AS open_managed_trades,
            (SELECT max(observed_at) FROM trade_management.trade_observation) AS latest_observation_at,
            (SELECT max(decision_time) FROM trade_management.trade_manager_decision) AS latest_decision_at,
            (SELECT greatest(reltuples, 0)::bigint FROM pg_class
             WHERE oid = to_regclass('trade_management.trade_observation')) AS observation_count,
            (SELECT greatest(reltuples, 0)::bigint FROM pg_class
             WHERE oid = to_regclass('trade_management.trade_manager_decision')) AS decision_count,
            (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'PUBLISHED') AS published_decision_count,
            (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'WITHHELD') AS withheld_decision_count,
            (SELECT json_agg(v ORDER BY v.tm_version_id) FROM
                (SELECT tm_version_id, evaluator_id, label, status FROM trade_management.trade_manager_version) v) AS policy_versions""")
        return {**rows[0], "counts_estimated": ["observation_count", "decision_count"],
                "computed_at": datetime.now(timezone.utc).isoformat()}

    def strategy_trade_management(self, strategy_id: str) -> dict[str, Any] | None:
        rows = self._trade_management_query("""SELECT b.binding_id, b.strategy_id,
            b.strategy_instance_id, b.instrument, b.tm_version_id, b.valid_from,
            v.evaluator_id, v.label, v.status, v.manifest
            FROM trade_management.legacy_stream_binding b
            JOIN trade_management.trade_manager_version v ON v.tm_version_id = b.tm_version_id
            WHERE b.strategy_id = %s
            ORDER BY b.valid_from DESC, b.binding_id""", (strategy_id,))
        if not rows:
            return None
        return {"strategyId": strategy_id, "bindings": rows,
                "versions": sorted({r["tm_version_id"] for r in rows}),
                "source": "canonical_postgres"}

    def managed_trades(self, limit: int, offset: int, trade_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE mt.managed_trade_id = %s" if trade_id else ""
        params: tuple[Any, ...] = (trade_id,) if trade_id else (limit, offset)
        tail = "" if trade_id else " LIMIT %s OFFSET %s"
        return self._trade_management_query(f"""SELECT mt.managed_trade_id, mt.entry_signal_id,
            mt.strategy_id, mt.strategy_version, mt.strategy_ref, mt.instrument, mt.direction,
            mt.decision_time AS opened_at, mt.reference_entry_price AS entry, mt.initial_stop,
            mt.initial_target AS target, mt.state, mt.tm_version_id, mt.binding_resolution,
            mt.evidence_mode, mt.eligibility, mt.eligibility_reason, mt.record_mode,
            mt.last_observation_seq, mt.created_at, obs.observed_at AS latest_observation_at,
            dec.decision_id AS latest_decision_id, dec.action AS latest_decision,
            dec.reason_codes AS latest_reason_codes, dec.decision_time AS latest_decision_at,
            pub.outcome AS publication_outcome, pub.reason AS publication_reason
            FROM trade_management.managed_trade mt
            LEFT JOIN LATERAL (SELECT o.observed_at FROM trade_management.trade_observation o
              WHERE o.managed_trade_id = mt.managed_trade_id ORDER BY o.observation_seq DESC LIMIT 1) obs ON TRUE
            LEFT JOIN LATERAL (SELECT d.* FROM trade_management.trade_manager_decision d
              WHERE d.managed_trade_id = mt.managed_trade_id ORDER BY d.observation_seq DESC, d.persisted_at DESC LIMIT 1) dec ON TRUE
            LEFT JOIN trade_management.publication_decision pub ON pub.decision_id = dec.decision_id
            {where} ORDER BY mt.created_at DESC, mt.managed_trade_id{tail}""", params)

    def trade_decisions(self, trade_id: str) -> list[dict[str, Any]]:
        return self._trade_management_query("""SELECT d.decision_id, d.managed_trade_id, d.observation_id,
            d.observation_seq, d.action, d.parameters, d.reason_codes, d.decision_trace_ref,
            d.decision_time, d.persisted_at, d.data_status, d.record_mode,
            p.outcome AS publication_outcome, p.reason AS publication_reason, p.evaluated_at AS publication_evaluated_at
            FROM trade_management.trade_manager_decision d
            LEFT JOIN trade_management.publication_decision p ON p.decision_id = d.decision_id
            WHERE d.managed_trade_id = %s ORDER BY d.observation_seq DESC, d.persisted_at DESC""", (trade_id,))

    def runtime_setting_reader(self, key: str) -> Callable[[], str | None]:
        """Lazy reader for one platform.runtime_setting value (migration 029)."""
        def read() -> str | None:
            try:
                rows = self.query("SELECT value FROM platform.runtime_setting WHERE key = %s", (key,))
            except CanonicalSourceUnavailable:
                return None
            value = rows[0]["value"] if rows else None
            return value if isinstance(value, str) else None
        return read

    def _live_linkage_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Live linkage joins trade_management to execution_v2 and needs
        `execution_result.broker_position_id` (migration 022)."""
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT count(*) FROM platform.schema_migrations WHERE version IN (%s, %s)",
                                (TRADE_MANAGEMENT_SCHEMA_VERSION, BROKER_POSITION_SCHEMA_VERSION))
                    if cur.fetchone()[0] != 2:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schemas 013 and 022 are required")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL live trade linkage unavailable") from exc

    def broker_linked_managed_trades(self) -> list[dict[str, Any]]:
        """ManagedTrades with a FILLED V2 result carrying a broker position id or order ticket
        (trade_manager_live.position_identity decides which can be linked), plus
        the market observations recorded since that fill (close side: bid LONG / ask SHORT)."""
        return self._live_linkage_query("""SELECT mt.managed_trade_id, mt.entry_signal_id,
            mt.strategy_id, mt.strategy_version, mt.instrument, mt.direction,
            mt.reference_entry_price, mt.initial_stop, mt.initial_target, mt.tm_version_id,
            i.execution_intent_id, a.attempt_id, a.resource AS attempt_resource,
            r.execution_result_id, r.outcome, r.account_id, r.broker_order_id, r.broker_deal_id,
            r.broker_position_id, r.volume AS fill_volume, r.actual_price AS fill_price,
            r.submitted_at, r.confirmed_at,
            obs.observation_count, obs.latest_observed_at, obs.latest_quote_timestamp,
            obs.latest_bid, obs.latest_ask, obs.max_bid, obs.min_bid, obs.max_ask, obs.min_ask,
            dec.action AS latest_action, dec.reason_codes AS latest_reason_codes,
            dec.decision_time AS latest_decision_at
            FROM trade_management.managed_trade mt
            JOIN execution_v2.execution_intent i ON i.entry_signal_id = mt.entry_signal_id
            JOIN execution_v2.execution_attempt a ON a.execution_intent_id = i.execution_intent_id
            JOIN execution_v2.execution_result r ON r.attempt_id = a.attempt_id
            LEFT JOIN LATERAL (
                SELECT count(*) AS observation_count,
                       max(s.bid) AS max_bid, min(s.bid) AS min_bid,
                       max(s.ask) AS max_ask, min(s.ask) AS min_ask,
                       (array_agg(o.observed_at ORDER BY o.observation_seq DESC))[1] AS latest_observed_at,
                       (array_agg(s.source_timestamp ORDER BY o.observation_seq DESC))[1] AS latest_quote_timestamp,
                       (array_agg(s.bid ORDER BY o.observation_seq DESC))[1] AS latest_bid,
                       (array_agg(s.ask ORDER BY o.observation_seq DESC))[1] AS latest_ask
                FROM trade_management.trade_observation o
                JOIN trade_management.market_snapshot s ON s.market_snapshot_id = o.market_snapshot_id
                WHERE o.managed_trade_id = mt.managed_trade_id
                  AND o.observed_at >= COALESCE(r.confirmed_at, r.submitted_at, r.created_at)) obs ON TRUE
            LEFT JOIN LATERAL (SELECT d.action, d.reason_codes, d.decision_time
                FROM trade_management.trade_manager_decision d
                WHERE d.managed_trade_id = mt.managed_trade_id
                ORDER BY d.observation_seq DESC, d.persisted_at DESC LIMIT 1) dec ON TRUE
            WHERE r.outcome = 'FILLED' AND (r.broker_position_id IS NOT NULL OR r.broker_order_id IS NOT NULL)
            ORDER BY r.confirmed_at DESC NULLS LAST, mt.managed_trade_id""")

    def managed_trade_linkage_counts(self) -> dict[str, int]:
        """Classify every ManagedTrade by how far its broker lineage can be proven."""
        rows = self._live_linkage_query("""SELECT linkage, count(*) AS n FROM (
            SELECT CASE
                WHEN EXISTS (SELECT 1 FROM execution_v2.execution_intent i
                             JOIN execution_v2.execution_result r ON r.execution_intent_id = i.execution_intent_id
                             WHERE i.entry_signal_id = mt.entry_signal_id
                               AND r.outcome = 'FILLED' AND r.broker_position_id IS NOT NULL) THEN 'BROKER_LINKED'
                WHEN EXISTS (SELECT 1 FROM execution_v2.execution_intent i
                             JOIN execution_v2.execution_result r ON r.execution_intent_id = i.execution_intent_id
                             WHERE i.entry_signal_id = mt.entry_signal_id
                               AND r.outcome = 'FILLED' AND r.broker_order_id IS NOT NULL)
                     THEN 'BROKER_FILLED_ORDER_TICKET_ONLY'
                WHEN EXISTS (SELECT 1 FROM execution_v2.execution_intent i
                             JOIN execution_v2.execution_result r ON r.execution_intent_id = i.execution_intent_id
                             WHERE i.entry_signal_id = mt.entry_signal_id) THEN 'BROKER_RESULT_WITHOUT_POSITION_ID'
                WHEN EXISTS (SELECT 1 FROM execution_v2.execution_intent i
                             JOIN execution_v2.execution_attempt a ON a.execution_intent_id = i.execution_intent_id
                             WHERE i.entry_signal_id = mt.entry_signal_id) THEN 'ATTEMPT_WITHOUT_RESULT'
                WHEN EXISTS (SELECT 1 FROM execution_v2.execution_intent i
                             WHERE i.entry_signal_id = mt.entry_signal_id) THEN 'INTENT_NOT_SENT'
                ELSE 'NO_EXECUTION_INTENT' END AS linkage
            FROM trade_management.managed_trade mt) classified
            GROUP BY linkage""")
        return {row["linkage"]: int(row["n"]) for row in rows}

    def strategy_entry_outcome_report(
        self,
        strategy_id: str,
        outcome_type: str,
        instance_id: str | None = None,
        cutoff_metadata_key: str | None = None,
        display_name: str | None = None,
        observability_version: str = "entry-outcomes.v1",
    ) -> dict[str, Any]:
        """Build the canonical entry/outcome report for a registered strategy.

        The shared schema is intentionally strategy-neutral. Each strategy selects its
        own outcome type, while the database remains the single source of truth for the
        signal/outcome rows and instance scope.
        """
        signal_scope = " AND s.strategy_instance_id = %s" if instance_id else ""
        query_params: tuple[Any, ...] = (strategy_id, outcome_type)
        if instance_id:
            query_params += (instance_id,)
        rows = self.query("""SELECT s.signal_id, s.economic_position_id,
                    s.instrument, s.direction, s.entry_price, s.decision_time,
                    o.status, o.realized_r, o.exit_timestamp, o.updated_at
                FROM strategy.entry_signals AS s
                JOIN strategy.entry_signal_outcomes AS o USING (signal_id)
                WHERE s.strategy_id = %s AND o.outcome_type = %s
                """ + signal_scope + " ORDER BY s.decision_time, s.signal_id""",
                          query_params)
        cutoff_rows = self.query("""SELECT value FROM platform.system_metadata
                                   WHERE key = %s""",
                                 (cutoff_metadata_key,)) if cutoff_metadata_key else []
        cutoff = cutoff_rows[0]["value"] if cutoff_rows else {}
        if isinstance(cutoff, str):
            cutoff = json.loads(cutoff)

        open_positions: list[dict[str, Any]] = []
        closed_positions: list[dict[str, Any]] = []
        by_symbol: dict[str, dict[str, Any]] = {}
        activity: list[dict[str, Any]] = []
        realized_values: list[float] = []
        for row in rows:
            outcome_status = row["status"]
            symbol = row["instrument"]
            symbol_data = by_symbol.setdefault(symbol, {
                "symbol": symbol, "detected": 0, "opportunities": 0,
                "entries": 0, "open": 0, "closed": 0, "realized_r": 0.0,
            })
            symbol_data["detected"] += 1
            symbol_data["opportunities"] += 1
            symbol_data["entries"] += 1
            position = {
                "signal_id": row["signal_id"],
                "economic_position_id": row["economic_position_id"],
                "symbol": symbol,
                "direction": row["direction"],
                "entry_time": row["decision_time"],
                "entry_price": row["entry_price"],
                "status": outcome_status,
            }
            realized_r = float(row["realized_r"]) if row["realized_r"] is not None else None
            if outcome_status == "OPEN":
                symbol_data["open"] += 1
                open_positions.append(position)
            elif realized_r is not None:
                realized_values.append(realized_r)
                symbol_data["closed"] += 1
                symbol_data["realized_r"] += realized_r
                closed_positions.append({
                    **position,
                    "outcome": outcome_status,
                    "close_time": row["exit_timestamp"],
                    "realized_r": realized_r,
                    "exit_reason": outcome_status,
                })
            activity.append({
                "timestamp": row["updated_at"],
                "strategy_id": strategy_id,
                "symbol": symbol,
                "event_type": outcome_status,
                "economic_position_id": row["economic_position_id"],
                "metadata": {"signal_id": row["signal_id"], "outcome_type": outcome_type},
            })

        closed_count = len(realized_values)
        target_count = sum(row["status"] == "TARGET_HIT" for row in rows)
        stopped_count = sum(row["status"] == "STOPPED" for row in rows)
        time_exit_count = sum(row["status"] == "TIME_EXIT" for row in rows)
        expired_count = sum(row["status"] == "EXPIRED" for row in rows)
        invalidated_count = sum(row["status"] == "INVALIDATED" for row in rows)
        realized_total = sum(realized_values)
        count = len(rows)
        cutoff_utc = cutoff.get("cutoff_utc") if isinstance(cutoff, dict) else None
        observed_at = datetime.now(timezone.utc).isoformat()
        return {
            "found": True,
            "report": {
                "identity": {
                    "strategy_id": strategy_id,
                    "strategy_instance_id": instance_id,
                    "display_name": display_name or strategy_id,
                    "strategy_version": "V1",
                    "observability_version": observability_version,
                    "sample_boundary": cutoff_utc,
                    "observed_at": observed_at,
                },
                "status": {
                    "runner_status": "UNKNOWN",
                    "observability_timestamp": observed_at,
                    "kill_switch": False,
                },
                "sample": {"scope": "CANONICAL_ENTRY_SIGNALS" if instance_id is None else "CANONICAL_ENTRY_SIGNALS_INSTANCE", "boundary": cutoff_utc},
                "funnel": [
                    {"stage": "ENTRY_SIGNALS", "label": "Canonical Entry Signals", "count": count},
                    {"stage": "OPEN", "label": "Open", "count": len(open_positions)},
                    {"stage": "TARGET_HIT", "label": "Target Hit", "count": target_count},
                    {"stage": "STOPPED", "label": "Stopped", "count": stopped_count},
                    {"stage": "TIME_EXIT", "label": "Time Exit", "count": time_exit_count},
                    {"stage": "EXPIRED", "label": "Expired", "count": expired_count},
                    {"stage": "INVALIDATED", "label": "Invalidated", "count": invalidated_count},
                ],
                "performance": {
                    "trades": closed_count,
                    "wins": target_count,
                    "losses": stopped_count,
                    "breakevens": 0,
                    "open": len(open_positions),
                    "realized_r": realized_total,
                    "expectancy_r": realized_total / len(realized_values) if realized_values else None,
                    "win_rate": target_count / len(realized_values) if realized_values else None,
                    "loss_rate": stopped_count / len(realized_values) if realized_values else None,
                },
                "symbols": list(by_symbol.values()),
                "open_positions": open_positions,
                "closed_positions": closed_positions,
                "rejection_reasons": [],
                "data_quality": {"gap_status": "NOT_TRACKED"},
                "recent_activity": sorted(activity, key=lambda item: item["timestamp"], reverse=True)[:50],
                "extension": {"kind": strategy_id},
                "outcome_authority": "canonical_postgres",
                "outcome_type": outcome_type,
                "outcome_schema_version": "031" if outcome_type == "LIQUIDITY_ENTRY" else "015",
            },
        }

    def context_entry_outcome_report(self, instance_id: str | None = None) -> dict[str, Any]:
        return self.strategy_entry_outcome_report(
            "CONTEXT_STRUCTURE_RETRACE_V1", "ENTRY_ONLY", instance_id,
            cutoff_metadata_key="context.entry_only_outcome_cutoff",
            display_name="Context Structure Retrace",
            observability_version="entry-only-outcomes.v1",
        )

    def liquidity_entry_outcome_report(self, instance_id: str | None = None) -> dict[str, Any]:
        return self.strategy_entry_outcome_report(
            "LIQUIDITY_DISPLACEMENT_SCALP_V1", "LIQUIDITY_ENTRY", instance_id,
            display_name="Liquidity Displacement Scalp",
            observability_version="liquidity-entry-outcomes.v1",
        )


STRATEGY_REPORT_REGISTRY: dict[str, dict[str, str]] = {
    "CONTEXT_STRUCTURE_RETRACE_V1": {
        "reader": "context_entry_outcome_report",
        "outcome_type": "ENTRY_ONLY",
    },
    "LIQUIDITY_DISPLACEMENT_SCALP_V1": {
        "reader": "liquidity_entry_outcome_report",
        "outcome_type": "LIQUIDITY_ENTRY",
    },
}


class PlatformControlApi:
    V2_RISK_POLICY_PATH = "/api/v1/v2-execution/risk-policy"
    V2_AUTHORITY_PATH = "/api/v1/v2-execution/authority"
    V2_CANARY_WINDOW_PATH = "/api/v1/v2-execution/canary-windows"
    TRADE_MANAGER_MODE_PATH = "/api/v1/trade-manager/mode"

    def __init__(self, repository: PlatformControlRepository | None = None,
                 environ: dict[str, str] | None = None,
                 strategy_config_path: str | None = None,
                 v2_risk_api: Any | None = None,
                 bridge_reader: Any | None = None,
                 trade_manager_mode_api: Any | None = None):
        self.repository = repository or PlatformControlRepository()
        self.instrument_membership = InstrumentMembershipRepository()
        from .strategy_catalog import StrategyCatalogRepository
        self.strategy_catalog = StrategyCatalogRepository()
        self.environ = os.environ if environ is None else environ
        # platform.json is no longer read. `strategy_config_path` is accepted for compatibility only.
        self.strategy_config_path = strategy_config_path
        if bridge_reader is None:
            # Live projections issue account, positions, and orders reads concurrently;
            # keep a stalled bridge from holding the API request open for tens of seconds.
            timeout = float(self.environ.get("MT5_BRIDGE_READ_TIMEOUT_SECONDS", "5"))
            setting = getattr(self.repository, "runtime_setting_reader", None)
            bridge_reader = ReadOnlyBridgeReader(self.environ.get("MT5_BRIDGE_MCP_URL")
                                                 or (setting("mcp_url") if setting else None), timeout=timeout)
            redis_url = self.environ.get("BROKER_VIEW_REDIS_URL", "").strip()
            if redis_url:
                # Broker state is served from the populator's Redis view (broker_view.service);
                # the bridge is read only when that view is missing or stale.
                import redis
                from broker_view.reader import RedisFirstBridgeReader
                from broker_view.store import BrokerViewStore
                store = BrokerViewStore(redis.Redis.from_url(redis_url, socket_timeout=0.5,
                                                             socket_connect_timeout=0.5))
                bridge_reader = RedisFirstBridgeReader(
                    store, bridge_reader, allowed_tools={name for name, _ in READ_ONLY_BROKER_TOOLS.values()},
                    max_age_seconds=float(self.environ.get("BROKER_VIEW_MAX_AGE_SECONDS", "45")))
        self.bridge_reader = bridge_reader
        if v2_risk_api is None:
            from .v2_risk import V2RiskExecutionApi
            v2_risk_api = V2RiskExecutionApi(environ=self.environ)
        self.v2_risk_api = v2_risk_api
        from .execution_authority import ExecutionAuthorityApi
        self.execution_authority_api = ExecutionAuthorityApi(runtime_status_fn=self.repository.execution_runtime_status)
        if trade_manager_mode_api is None:
            from .trade_manager_mode import TradeManagerModeApi
            trade_manager_mode_api = TradeManagerModeApi()
        self.trade_manager_mode_api = trade_manager_mode_api

    @staticmethod
    def _strategy_write_route(path: str) -> tuple[str, str] | None:
        """/api/v1/strategies/{strategyId}/(lifecycle|metadata|manifest) -> (strategyId, action)."""
        prefix = "/api/v1/strategies/"
        if not path.startswith(prefix):
            return None
        parts = [unquote(p) for p in path[len(prefix):].split("/")]
        if len(parts) == 2 and parts[0] and parts[1] in ("lifecycle", "metadata", "manifest"):
            return parts[0], parts[1]
        return None

    def _save_strategy(self, strategy_id: str, action: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        from .strategy_catalog import (InstanceNotFound, InstanceRevisionConflict, LifecycleNotEnforced,
                                       ManifestNotAvailable)
        source = "canonical_postgres"
        try:
            payload = json.loads((body or b"{}").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if action == "manifest":
                row = self.strategy_catalog.publish_manifest(strategy_id,
                                                             updated_by=str(payload.get("updatedBy") or "control-api"))
                return 200, self._body(row, source=source)
            common = {"expected_revision": payload.get("expectedRevision"),
                      "updated_by": str(payload.get("updatedBy") or "control-api")}
            if action == "lifecycle":
                row = self.strategy_catalog.set_strategy_lifecycle(strategy_id, payload.get("state"), **common)
            else:
                row = self.strategy_catalog.set_strategy_metadata(
                    strategy_id, display_name=payload.get("displayName"), description=payload.get("description"),
                    **common)
            return 200, self._body(row, source=source)
        except InstanceNotFound as exc:
            return 404, self._body(None, source=source, error="RESOURCE_NOT_FOUND", message=str(exc))
        except LifecycleNotEnforced as exc:
            return 409, self._body(None, source=source, error="LIFECYCLE_NOT_ENFORCED", message=str(exc))
        except ManifestNotAvailable as exc:
            return 409, self._body(None, source=source, error="MANIFEST_NOT_AVAILABLE", message=str(exc))
        except InstanceRevisionConflict as exc:
            return 409, self._body(None, source=source, error="REVISION_CONFLICT", message=str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            return 400, self._body(None, source=source, error="INVALID_REQUEST", message=str(exc))
        except CanonicalSourceUnavailable as exc:
            return 503, self._body(None, source=source, status="UNAVAILABLE", error="SOURCE_UNAVAILABLE",
                                   message=str(exc))

    @staticmethod
    def _instance_lifecycle_route(path: str) -> tuple[str, str] | None:
        """/api/v1/strategies/{strategyId}/instances/{instanceId}/lifecycle -> (strategyId, instanceId)."""
        prefix = "/api/v1/strategies/"
        if not path.startswith(prefix):
            return None
        parts = [unquote(p) for p in path[len(prefix):].split("/")]
        if len(parts) == 4 and parts[1] == "instances" and parts[3] == "lifecycle" and parts[0] and parts[2]:
            return parts[0], parts[2]
        return None

    def _save_instance_lifecycle(self, strategy_id: str, instance_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        from .strategy_catalog import (InstanceNotFound, InstanceRevisionConflict, LifecycleNotEnforced,
                                       ParameterSetNotPublished)
        source = "canonical_postgres"
        try:
            payload = json.loads((body or b"{}").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            row = self.strategy_catalog.set_instance_lifecycle(
                strategy_id, instance_id, payload.get("state"),
                expected_revision=payload.get("expectedRevision"),
                updated_by=str(payload.get("updatedBy") or "control-api"))
            return 200, self._body(row, source=source)
        except InstanceNotFound as exc:
            return 404, self._body(None, source=source, error="RESOURCE_NOT_FOUND", message=str(exc))
        except LifecycleNotEnforced as exc:
            return 409, self._body(None, source=source, error="LIFECYCLE_NOT_ENFORCED", message=str(exc))
        except ParameterSetNotPublished as exc:
            return 409, self._body(None, source=source, error="PARAMETER_SET_NOT_PUBLISHED", message=str(exc))
        except InstanceRevisionConflict as exc:
            return 409, self._body(None, source=source, error="REVISION_CONFLICT", message=str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            return 400, self._body(None, source=source, error="INVALID_REQUEST", message=str(exc))
        except CanonicalSourceUnavailable as exc:
            return 503, self._body(None, source=source, status="UNAVAILABLE", error="SOURCE_UNAVAILABLE",
                                   message=str(exc))

    @staticmethod
    def _body(data: Any = None, *, source: str, status: str = "ACTIVE",
              error: str | None = None, message: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"api_version": "v1", "source": source,
                                  "status": status, "degraded": status in {"DEGRADED", "UNAVAILABLE"},
                                  "read_only": True}
        if error:
            result.update(error=error, message=message or error,
                          unavailable=[{"code": error, "source": source, "message": message or error}])
        else:
            result.update(data=data, unavailable=[])
        return result

    @staticmethod
    def _authority(env: dict[str, str]) -> dict[str, str]:
        return {"orchestrator_mode": env.get("ORCHESTRATOR_MODE", "UNKNOWN"),
                "signal_authority_mode": env.get("SIGNAL_AUTHORITY_MODE", "UNKNOWN"),
                "execution_authority_mode": env.get("EXECUTION_AUTHORITY_MODE", "UNKNOWN")}

    def _execution_state(self) -> dict[str, Any]:
        runtime = self.repository.execution_runtime_status()
        from execution_v2.authority_store import read_authority
        authority = read_authority()
        policy = dict(runtime.get("risk_policy") or {})
        policy.setdefault("enabled", False)
        return {
            "execution_authority_mode": authority.get("state", "DISABLED"),
            "authority_revision": authority.get("revision", 0),
            "authority_source": authority.get("source", "POSTGRES"),
            "risk_policy": policy,
            "canary": runtime["canary"],
            "execution_worker": {"status": runtime["worker_status"]},
            "execution_bridge": runtime["execution_bridge"],
            "broker_account": runtime["broker_account"],
            "source": "canonical_execution_runtime",
        }

    def _database(self, *, include_event_counts: bool = True) -> dict[str, Any]:
        if include_event_counts:
            return self.repository.platform_status()
        return self.repository.platform_status(include_event_counts=False)

    def execute(self, method: str, target: str, body: bytes | None = None) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/v1/instruments" and method == "GET":
            try:
                return 200, self._body([item.__dict__ for item in self.instrument_membership.list_catalog()],
                                       source="canonical_postgres")
            except CanonicalSourceUnavailable as exc:
                return 503, self._body(None, source="canonical_postgres", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message=str(exc))
        if path.startswith("/api/v1/strategies/") and path.endswith("/instruments"):
            strategy_id = unquote(path[len("/api/v1/strategies/"):-len("/instruments")].strip("/"))
            query_values = parse_qs(parsed.query, keep_blank_values=False)
            instance_id = (query_values.get("strategy_instance_id") or
                            [DEFAULT_INSTANCE_BY_STRATEGY.get(strategy_id, "")])[0]
            if not instance_id:
                return 400, self._body(None, source="canonical_postgres", error="INSTANCE_REQUIRED")
            if method == "GET":
                try:
                    rows = self.instrument_membership.list_membership(strategy_id, instance_id)
                    return 200, self._body({"strategyId": strategy_id, "strategyInstanceId": instance_id,
                                           "items": rows, "configuredRevision": max((int(r["revision"]) for r in rows), default=0)},
                                          source="canonical_postgres")
                except CanonicalSourceUnavailable as exc:
                    return 503, self._body(None, source="canonical_postgres", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE", message=str(exc))
            if method == "POST":
                try:
                    payload = json.loads((body or b"{}").decode("utf-8"))
                    canonical = str(payload.get("canonicalInstrument", "")).strip().upper()
                    state = str(payload.get("state") or ("ACTIVE" if payload.get("enable") else "DISABLED")).upper()
                    catalog = {x.canonical_instrument: x for x in self.instrument_membership.list_catalog()}
                    if canonical not in catalog:
                        raise UnsupportedInstrument(f"{canonical} has no active provider mapping in the instrument catalog")
                    if state == "ACTIVE" and not catalog[canonical].provider_symbol:
                        raise UnsupportedInstrument(f"{canonical} has no provider mapping")
                    self.strategy_catalog.check_membership_change(strategy_id, instance_id, canonical, state)
                    row = self.instrument_membership.save_membership(
                        strategy_id, instance_id, canonical, state,
                        payload.get("expectedRevision"), str(payload.get("updatedBy") or "control-api"))
                    return 200, self._body(row, source="canonical_postgres")
                except UnsupportedInstrument as exc:
                    return 409, self._body(None, source="canonical_postgres", status="DEGRADED",
                                           error="UNSUPPORTED_INSTRUMENT", message=str(exc))
                except MembershipRefused as exc:
                    return 409, self._body(None, source="canonical_postgres", error=exc.code, message=str(exc))
                except MembershipConflict as exc:
                    return 409, self._body(None, source="canonical_postgres", error="REVISION_CONFLICT", message=str(exc))
                except (ValueError, json.JSONDecodeError) as exc:
                    return 400, self._body(None, source="canonical_postgres", error="INVALID_REQUEST", message=str(exc))
                except CanonicalSourceUnavailable as exc:
                    return 503, self._body(None, source="canonical_postgres", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE", message=str(exc))
            return 405, self._body(None, source="platform", error="METHOD_NOT_ALLOWED")
        strategy_write = self._strategy_write_route(path)
        if strategy_write is not None:
            if method != "POST":
                return 405, self._body(None, source="platform", error="METHOD_NOT_ALLOWED")
            return self._save_strategy(*strategy_write, body)
        lifecycle = self._instance_lifecycle_route(path)
        if lifecycle is not None:
            if method != "POST":
                return 405, self._body(None, source="platform", error="METHOD_NOT_ALLOWED")
            return self._save_instance_lifecycle(*lifecycle, body)
        if method == "POST" and path == self.V2_RISK_POLICY_PATH:
            return self.v2_risk_api.save(body)
        if path == self.V2_AUTHORITY_PATH:
            if method == "GET":
                return self.execution_authority_api.read()
            if method == "POST":
                return self.execution_authority_api.save(body)
        if path == self.TRADE_MANAGER_MODE_PATH:
            if method == "GET":
                return self.trade_manager_mode_api.read()
            if method == "POST":
                return self.trade_manager_mode_api.save(body)
        if method == "POST" and path == self.V2_CANARY_WINDOW_PATH:
            return self.v2_risk_api.open_window(body)
        if method != "GET":
            return 405, self._body(None, source="platform", status="UNAVAILABLE", error="READ_ONLY_API",
                                   message="GET only" if path != self.V2_RISK_POLICY_PATH else "GET or POST only")
        query_values = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=32)
        query = {k: v[-1] for k, v in query_values.items()}
        try:
            if path == self.V2_RISK_POLICY_PATH:
                return self.v2_risk_api.read()
            if path == "/healthz":
                return 200, {"status": "ok", "service": "platform-control-api"}
            if path == "/readyz":
                self._database(include_event_counts=False)
                return 200, {"status": "ready", "service": "platform-control-api",
                             "source": "canonical_postgres", "schema_version": SCHEMA_VERSION}
            if path == "/api/v1/system":
                core_view = query.get("view") == "core"
                db = self._database(include_event_counts=not core_view)
                execution = self._execution_state()
                if core_view:
                    trade_manager = {"status": "DEFERRED", "reason": "fetch /trade-manager/summary separately"}
                else:
                    try:
                        tm_reader = getattr(self.repository, "trade_manager_summary", None)
                        if tm_reader is None:
                            raise CanonicalSourceUnavailable("canonical Trade Manager observability unavailable")
                        tm = tm_reader()
                        trade_manager = {"status": "ACTIVE", "source": "canonical_postgres",
                                         "managed_trade_count": tm["total_managed_trades"],
                                         "open_managed_trade_count": tm["open_managed_trades"]}
                    except CanonicalSourceUnavailable:
                        trade_manager = {"status": "UNKNOWN", "reason": "canonical Trade Manager observability unavailable"}
                authority = {**self._authority(self.environ),
                             "execution_authority_mode": execution["execution_authority_mode"]}
                runtime = "ACTIVE" if db["orchestrator_running"] else "UNKNOWN"
                state = {**authority, "components": {
                    "orchestrator": {"status": runtime, "configured_mode": authority["orchestrator_mode"]},
                    "postgresql": {"status": "ACTIVE", "schema_version": SCHEMA_VERSION},
                    "signal_authority": {"status": "ACTIVE" if authority["signal_authority_mode"] == "DB_PRIMARY" else "DEGRADED"},
                    "jetstream": {"status": "UNKNOWN", "reason": "health is not asserted by the Control API"},
                    "trade_manager": trade_manager,
                    "execution": {"status": "ACTIVE" if authority["execution_authority_mode"] == "ENABLED" else "INACTIVE",
                                   **execution}},
                    "canonical_outbox_event_count": db["outbox_count"], "canonical_inbox_event_count": db["inbox_count"]}
                return 200, self._body(state, source="canonical_platform")
            if path == "/api/v1/safety":
                self._database(include_event_counts=False)  # Canonical-source reachability/schema is required to make this assertion.
                execution = self._execution_state()
                authority = {**self._authority(self.environ),
                             "execution_authority_mode": execution["execution_authority_mode"]}
                safe = (authority["orchestrator_mode"] == "PRIMARY"
                        and authority["signal_authority_mode"] == "DB_PRIMARY"
                        and self.environ.get("SIGNAL_DB_PRIMARY_ENABLED", "").lower() == "true")
                if not safe:
                    data = {**authority, "status": "BLOCKED", "execution_enabled": False,
                            "real_execution_mode_active": False, "broker_write_path_active": False,
                            "blockers": ["CANONICAL_AUTHORITY_CONFIGURATION_NOT_CONFIRMED"],
                            "execution": execution}
                    return 200, self._body(data, source="canonical_platform", status="DEGRADED")
                armed = (execution["execution_authority_mode"] == "ENABLED"
                         and bool(execution["risk_policy"].get("enabled")))
                data = {**authority, "status": "ARMED" if armed else "SAFE",
                        "execution_enabled": execution["execution_authority_mode"] == "ENABLED",
                        "real_execution": {"armed": armed, "mode": execution["execution_authority_mode"]},
                        "canonical_order_send_gate": {"effective": execution["execution_authority_mode"]},
                        "execution_consumer": {"status": execution["execution_worker"]["status"]},
                        "real_execution_mode_active": execution["execution_authority_mode"] == "ENABLED",
                        "broker_write_path_active": execution["execution_authority_mode"] == "ENABLED",
                        "blockers": [], "execution": execution}
                return 200, self._body(data, source="canonical_platform")
            if path == "/api/v1/strategies" or path.startswith("/api/v1/strategies/"):
                # Strategy definitions and every statistic come from PostgreSQL (migration 028);
                # the detail is the complete page model, computed over full history.
                suffix = path[len("/api/v1/strategies"):].strip("/")
                if not suffix:
                    return 200, self._body(self.strategy_catalog.list_strategies(), source="canonical_postgres")
                parts = [unquote(p) for p in suffix.split("/")]
                if len(parts) == 1:
                    page = self.strategy_catalog.strategy_page(parts[0])
                    if page is None:
                        return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                    return 200, self._body(page, source="canonical_postgres")
                if len(parts) == 2 and parts[1] == "instances":
                    page = self.strategy_catalog.strategy_page(parts[0])
                    if page is None:
                        return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                    return 200, self._body(self.strategy_catalog.strategy_instances(parts[0]),
                                           source="canonical_postgres")
                if len(parts) == 3 and parts[1] == "instances":
                    page = self.strategy_catalog.instance_page(parts[0], parts[2])
                    if page is None:
                        return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                    return 200, self._body(page, source="canonical_postgres")
                report_config = STRATEGY_REPORT_REGISTRY.get(parts[0]) if parts else None
                if len(parts) == 4 and parts[1] == "instances" and parts[3] == "report" and report_config:
                    page = self.strategy_catalog.instance_page(parts[0], parts[2])
                    if page is None:
                        return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                    report = getattr(self.repository, report_config["reader"])(instance_id=parts[2])
                    return 200, self._body(report, source="canonical_postgres")
                if len(parts) == 2 and parts[1] == "report" and report_config:
                    report = getattr(self.repository, report_config["reader"])()
                    return 200, self._body(report, source="canonical_postgres")
                return 503, self._body(None, source="canonical_platform", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message="Canonical strategy observability data is not available")
            if path == "/api/v1/events" or path.startswith("/api/v1/events/"):
                limit = min(max(int(query.get("limit", LIMIT)), 1), 500)
                offset = max(int(query.get("offset", 0)), 0)
                suffix = path[len("/api/v1/events/"):] if path.startswith("/api/v1/events/") else None
                rows = self.repository.events(limit, offset, unquote(suffix) if suffix else None)
                if suffix and not rows:
                    return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                data = [{**r, "timestamp": r.get("occurred_at"), "event_type": r.get("event_type"),
                         "strategy_id": (r.get("payload") or {}).get("strategy_id") if isinstance(r.get("payload"), dict) else None,
                         "signal_id": (r.get("payload") or {}).get("signal_id") if isinstance(r.get("payload"), dict) else None}
                        for r in rows]
                return 200, self._body(data[0] if suffix else data, source="canonical_postgres")
            if path == "/api/v1/audit" or path.startswith("/api/v1/audit/"):
                return 503, self._body(None, source="canonical_platform_audit", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message="No canonical administrative audit source is available")
            if path == "/api/v1/executions" or path.startswith("/api/v1/executions/"):
                suffix = path[len("/api/v1/executions/"):] if path.startswith("/api/v1/executions/") else None
                if suffix == "metrics":
                    metrics = self.repository.execution_metrics()
                    return 200, self._body(metrics, source="canonical_postgres",
                                           status="INACTIVE" if self._authority(self.environ)["execution_authority_mode"] == "DISABLED" else "ACTIVE")
                rows = self.repository.executions(min(max(int(query.get("limit", LIMIT)), 1), 500),
                                                  max(int(query.get("offset", 0)), 0), unquote(suffix) if suffix else None)
                if suffix and not rows:
                    return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                return 200, self._body(rows[0] if suffix else {"executions": rows}, source="canonical_postgres",
                                       status="INACTIVE" if self._authority(self.environ)["execution_authority_mode"] == "DISABLED" else "ACTIVE")
            if path == "/api/v1/connections":
                self._database(include_event_counts=False)   # reachability only
                execution = self._execution_state()
                exec_mode = execution["execution_authority_mode"]
                # Execution channel: port 22348 (execution_bridge.py) — status from runtime.
                exec_bridge = execution.get("execution_bridge") or {}
                exec_bridge_status = exec_bridge.get("status", "UNKNOWN")
                if exec_mode == "ENABLED":
                    exec_channel = {"status": exec_bridge_status, "source": "execution_bridge",
                                    "mode": exec_mode}
                else:
                    exec_channel = {"status": "INACTIVE", "reason": "execution authority disabled",
                                    "mode": exec_mode}
                # Data channel: port 22347 (bridge.py) — endpoint lives in bridge_reader.
                # RedisFirstBridgeReader wraps the raw ReadOnlyBridgeReader as .fallback.
                raw_reader = getattr(self.bridge_reader, "fallback", self.bridge_reader)
                data_endpoint = getattr(raw_reader, "endpoint", None)
                data_status = "UP" if data_endpoint else "UNAVAILABLE"
                data_channel = {"status": data_status, "source": "mt5_bridge_read_only",
                                "endpoint": data_endpoint}
                healthy = {"UP", "HEALTHY", "ACTIVE"}
                overall = "ACTIVE" if data_status in healthy and exec_bridge_status in healthy else "DEGRADED"
                return 200, self._body({"data_channel": data_channel, "execution_channel": exec_channel},
                                       source="platform_and_bridge", status=overall)
            if path == "/api/v1/trade-manager/summary":
                return 200, self._body(self.repository.trade_manager_summary(), source="canonical_postgres")
            if path == "/api/v1/trade-manager/live":
                from .trade_manager_live import TradeManagerLiveProjection
                projection = TradeManagerLiveProjection(
                    self.repository, self.bridge_reader,
                    stale_after_seconds=float(self.environ.get("TM_LIVE_OBSERVATION_STALE_SECONDS", "120"))).project()
                mode = self.trade_manager_mode_api.read()[1].get("data") or {}
                projection["trade_manager_mode"] = {"mode": mode.get("mode", "OFF"), "revision": mode.get("revision", 0),
                                                    "error": mode.get("error")}
                return 200, self._body(projection, source="canonical_postgres+mt5_bridge_read_only",
                                       status="ACTIVE" if projection["system_state"] == "LIVE" else "DEGRADED")
            if path.startswith("/api/v1/managed-trades/") and path.endswith("/decisions"):
                trade_id = unquote(path[len("/api/v1/managed-trades/"):-len("/decisions")].strip("/"))
                return 200, self._body(self.repository.trade_decisions(trade_id), source="canonical_postgres")
            if path == "/api/v1/managed-trades" or (path.startswith("/api/v1/managed-trades/") and not path.endswith("/decisions")):
                suffix = path[len("/api/v1/managed-trades/"):] if path.startswith("/api/v1/managed-trades/") else None
                limit = min(max(int(query.get("limit", LIMIT)), 1), 500)
                offset = max(int(query.get("offset", 0)), 0)
                rows = self.repository.managed_trades(limit, offset, unquote(suffix) if suffix else None)
                if suffix and not rows:
                    return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND",
                                           message=f"managed trade {unquote(suffix)} not found")
                return 200, self._body(rows[0] if suffix else rows, source="canonical_postgres")
            broker_paths = {"/api/v1/broker/account", "/api/v1/broker/positions", "/api/v1/broker/pending-orders",
                            "/api/v1/broker/history-orders", "/api/v1/broker/deals", "/api/v1/broker/symbols",
                            "/api/v1/broker/exposure", "/api/v1/exposure"}
            if path in broker_paths:
                resource = path.rsplit("/", 1)[-1]
                if path == "/api/v1/exposure":
                    resource = "positions"
                tool, arguments = READ_ONLY_BROKER_TOOLS.get(resource, (None, None))
                if tool is None:
                    return 503, self._body(None, source="mt5_bridge_read_only", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE", message="Read-only MT5 bridge resource is unavailable")
                try:
                    read = getattr(self.bridge_reader, "read", None)
                    if read is None:
                        return 200, self._body(self.bridge_reader.call(tool, arguments), source="mt5_bridge_read_only")
                    data, observed_at, source = read(tool, arguments)
                    body = self._body(data, source=source)
                    body["observed_at"] = datetime.fromtimestamp(observed_at, timezone.utc).isoformat()
                    return 200, body
                except Exception as exc:
                    return 503, self._body(None, source="mt5_bridge_read_only", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE",
                                           message=f"Read-only MT5 bridge is unavailable: {exc}")
            if path == "/api/v1/reports" or path.startswith("/api/v1/reports/"):
                report_reader = getattr(self.repository, "context_entry_outcome_report", None)
                if report_reader is None:
                    return 503, self._body(None, source="canonical_platform_reports", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE", message="No canonical report registry is available")
                report = report_reader()
                report_id = path[len("/api/v1/reports/"):].strip("/") if path.startswith("/api/v1/reports/") else ""
                if "identity" not in report.get("report", {}):
                    return 503, self._body(None, source="canonical_platform_reports", status="UNAVAILABLE",
                                           error="SOURCE_UNAVAILABLE", message="Canonical report projection unavailable")
                summary = {"id": "context-entry-outcomes", "type": "STRATEGY_PERFORMANCE",
                           "title": "Context Structure Retrace — Entry Outcomes",
                           "generated_at": report["report"]["identity"]["observed_at"],
                           "status": "READY", "summary": "Canonical PostgreSQL ENTRY_ONLY outcome report."}
                if report_id and report_id != summary["id"]:
                    return 404, self._body(None, source="canonical_postgres", error="RESOURCE_NOT_FOUND")
                return 200, self._body({**summary, "report": report["report"]} if report_id else [summary],
                                       source="canonical_postgres")
            if path.startswith("/api/v1/"):
                return 404, self._body(None, source="platform_api_router", status="UNAVAILABLE", error="RESOURCE_NOT_FOUND")
            return 404, self._body(None, source="platform_api_router", status="UNAVAILABLE", error="RESOURCE_NOT_FOUND")
        except CanonicalSourceUnavailable as exc:
            return 503, self._body(None, source="canonical_postgres", status="UNAVAILABLE",
                                   error="SOURCE_UNAVAILABLE", message=str(exc))
        except (ValueError, TypeError):
            return 400, self._body(None, source="canonical_platform", status="UNAVAILABLE", error="INVALID_QUERY")
