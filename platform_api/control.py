"""Read-only platform-owned Control API routes.

This module intentionally has no filesystem runtime-state fallback.  Current
authority comes from the deployed authority environment, canonical PostgreSQL,
and (for the active strategy definition only) the mounted platform config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

SCHEMA_VERSION = "012"
TRADE_MANAGEMENT_SCHEMA_VERSION = "013"
EXECUTION_RUNTIME_COMPONENT = "execution_v2"
LIMIT = 100


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

    def platform_status(self) -> dict[str, Any]:
        rows = self.query("""SELECT
            (SELECT count(*) FROM platform.outbox_events) AS outbox_count,
            (SELECT count(*) FROM platform.inbox_events) AS inbox_count,
            (SELECT count(*) FROM platform.runtime_instances WHERE component = 'orchestrator' AND status = 'RUNNING') AS orchestrator_running""")
        return rows[0]

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
        canary_key = metadata.get("canary_key")
        canary = None
        if canary_key:
            canary_rows = self.query("""SELECT max_new_executions, consumed
                FROM execution_v2.canary_state WHERE canary_key = %s""", (canary_key,))
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
            "canary": {"max_new_executions": max_new, "consumed": consumed,
                       "remaining": max(0, max_new - consumed)},
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
        rows = self._trade_management_query("""SELECT
            (SELECT count(*) FROM trade_management.managed_trade) AS total_managed_trades,
            (SELECT count(*) FROM trade_management.managed_trade WHERE state = 'OPEN') AS open_managed_trades,
            (SELECT max(observed_at) FROM trade_management.trade_observation) AS latest_observation_at,
            (SELECT max(decision_time) FROM trade_management.trade_manager_decision) AS latest_decision_at,
            (SELECT count(*) FROM trade_management.trade_observation) AS observation_count,
            (SELECT count(*) FROM trade_management.trade_manager_decision) AS decision_count,
            (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'PUBLISHED') AS published_decision_count,
            (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'WITHHELD') AS withheld_decision_count,
            (SELECT json_agg(v ORDER BY v.tm_version_id) FROM
                (SELECT tm_version_id, evaluator_id, label, status FROM trade_management.trade_manager_version) v) AS policy_versions""")
        return rows[0]

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

    def context_entry_outcome_report(self) -> dict[str, Any]:
        """Build the Context ENTRY_ONLY report strictly from PostgreSQL rows."""
        rows = self.query("""SELECT s.signal_id, s.economic_position_id,
                    s.instrument, s.direction, s.entry_price, s.decision_time,
                    o.status, o.realized_r, o.exit_timestamp, o.updated_at
                FROM strategy.entry_signals AS s
                JOIN strategy.entry_signal_outcomes AS o USING (signal_id)
                WHERE s.strategy_id = %s AND o.outcome_type = %s
                ORDER BY s.decision_time, s.signal_id""",
                          ("CONTEXT_STRUCTURE_RETRACE_V1", "ENTRY_ONLY"))
        cutoff_rows = self.query("""SELECT value FROM platform.system_metadata
                                   WHERE key = %s""",
                                 ("context.entry_only_outcome_cutoff",))
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
            if outcome_status == "OPEN":
                symbol_data["open"] += 1
                open_positions.append(position)
            else:
                realized_r = float(row["realized_r"])
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
                "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                "symbol": symbol,
                "event_type": outcome_status,
                "economic_position_id": row["economic_position_id"],
                "metadata": {"signal_id": row["signal_id"], "outcome_type": "ENTRY_ONLY"},
            })

        closed_count = len(closed_positions)
        target_count = sum(row["status"] == "TARGET_HIT" for row in rows)
        stopped_count = sum(row["status"] == "STOPPED" for row in rows)
        realized_total = sum(realized_values)
        count = len(rows)
        cutoff_utc = cutoff.get("cutoff_utc") if isinstance(cutoff, dict) else None
        observed_at = datetime.now(timezone.utc).isoformat()
        return {
            "found": True,
            "report": {
                "identity": {
                    "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                    "display_name": "Context Structure Retrace",
                    "strategy_version": "V1",
                    "observability_version": "entry-only-outcomes.v1",
                    "sample_boundary": cutoff_utc,
                    "observed_at": observed_at,
                },
                "status": {
                    "runner_status": "UNKNOWN",
                    "observability_timestamp": observed_at,
                    "kill_switch": False,
                },
                "sample": {"scope": "CANONICAL_POST_T0_ENTRY_SIGNALS", "boundary": cutoff_utc},
                "funnel": [
                    {"stage": "ENTRY_SIGNALS", "label": "Canonical Entry Signals", "count": count},
                    {"stage": "OPEN", "label": "Open", "count": len(open_positions)},
                    {"stage": "TARGET_HIT", "label": "Target Hit", "count": target_count},
                    {"stage": "STOPPED", "label": "Stopped", "count": stopped_count},
                ],
                "performance": {
                    "trades": closed_count,
                    "wins": target_count,
                    "losses": stopped_count,
                    "breakevens": 0,
                    "open": len(open_positions),
                    "realized_r": realized_total,
                    "expectancy_r": realized_total / closed_count if closed_count else None,
                    "win_rate": target_count / closed_count if closed_count else None,
                    "loss_rate": stopped_count / closed_count if closed_count else None,
                },
                "symbols": list(by_symbol.values()),
                "open_positions": open_positions,
                "closed_positions": closed_positions,
                "rejection_reasons": [],
                "data_quality": {"gap_status": "NOT_TRACKED"},
                "recent_activity": sorted(activity, key=lambda item: item["timestamp"], reverse=True)[:50],
                "extension": {"kind": "CONTEXT_STRUCTURE_RETRACE_V1"},
                "outcome_authority": "canonical_postgres",
                "outcome_type": "ENTRY_ONLY",
                "outcome_schema_version": "015",
            },
        }


class PlatformControlApi:
    V2_RISK_POLICY_PATH = "/api/v1/v2-execution/risk-policy"
    V2_AUTHORITY_PATH = "/api/v1/v2-execution/authority"

    def __init__(self, repository: PlatformControlRepository | None = None,
                 environ: dict[str, str] | None = None,
                 strategy_config_path: str | None = None,
                 v2_risk_api: Any | None = None):
        self.repository = repository or PlatformControlRepository()
        self.environ = os.environ if environ is None else environ
        self.strategy_config_path = strategy_config_path or self.environ.get(
            "PLATFORM_STRATEGY_CONFIG_PATH", "/etc/platform-config/platform.json")
        if v2_risk_api is None:
            from .v2_risk import V2RiskExecutionApi
            v2_risk_api = V2RiskExecutionApi(environ=self.environ)
        self.v2_risk_api = v2_risk_api
        from .execution_authority import ExecutionAuthorityApi
        self.execution_authority_api = ExecutionAuthorityApi(runtime_status_fn=self.repository.execution_runtime_status)

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

    def _database(self) -> dict[str, Any]:
        return self.repository.platform_status()

    def _strategies(self) -> list[dict[str, Any]]:
        try:
            config = json.loads(Path(self.strategy_config_path).read_text(encoding="utf-8"))
            items = config.get("strategies")
            if not isinstance(items, list):
                raise ValueError("platform config strategies must be a list")
            projected = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                projected.append({key: item[key] for key in (
                    "strategy_id", "strategy_version", "enabled", "adapter", "routes") if key in item})
            return projected
        except Exception as exc:
            raise CanonicalSourceUnavailable("active platform strategy configuration unavailable") from exc

    def execute(self, method: str, target: str, body: bytes | None = None) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        if method == "POST" and path == self.V2_RISK_POLICY_PATH:
            return self.v2_risk_api.save(body)
        if path == self.V2_AUTHORITY_PATH:
            if method == "GET":
                return self.execution_authority_api.read()
            if method == "POST":
                return self.execution_authority_api.save(body)
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
                self._database()
                return 200, {"status": "ready", "service": "platform-control-api",
                             "source": "canonical_postgres", "schema_version": SCHEMA_VERSION}
            if path == "/api/v1/system":
                db = self._database()
                execution = self._execution_state()
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
                self._database()  # Canonical-source reachability/schema is required to make this assertion.
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
                         and bool(execution["risk_policy"].get("enabled"))
                         and execution["canary"].get("remaining", 0) > 0)
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
                strategies = self._strategies()
                suffix = path[len("/api/v1/strategies"):].strip("/")
                if not suffix:
                    return 200, self._body(strategies, source="active_platform_config", status="ACTIVE")
                parts = [unquote(p) for p in suffix.split("/")]
                strategy = next((s for s in strategies if s.get("strategy_id") == parts[0]), None)
                if strategy is None:
                    return 404, self._body(None, source="active_platform_config", status="ACTIVE", error="RESOURCE_NOT_FOUND")
                if len(parts) == 1:
                    return 200, self._body(strategy, source="active_platform_config")
                if (parts[0] == "CONTEXT_STRUCTURE_RETRACE_V1" and len(parts) == 2
                        and parts[1] == "report"):
                    report = self.repository.context_entry_outcome_report()
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
                self._database()
                return 200, self._body({"data_channel": {"status": "UNAVAILABLE", "source": "mt5_bridge_read_only"},
                                        "execution_channel": {"status": "INACTIVE", "reason": "execution authority disabled"}}, source="platform_and_bridge", status="DEGRADED")
            if path == "/api/v1/trade-manager/summary":
                return 200, self._body(self.repository.trade_manager_summary(), source="canonical_postgres")
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
                return 503, self._body(None, source="mt5_bridge_read_only", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message="Read-only MT5 bridge is not reachable from the platform API")
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
