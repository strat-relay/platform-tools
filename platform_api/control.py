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
        # The ONE deliberate, narrow exception to this API's otherwise-total read-only posture
        # (mission CLAUDE-V2-RISK-EXECUTION-CONSOLE section 10) - see platform_api/v2_risk.py's
        # module docstring. Every other path, and every other method on THIS path, is unaffected.
        if method == "POST" and path == self.V2_RISK_POLICY_PATH:
            return self.v2_risk_api.save(body)
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
                authority = self._authority(self.environ)
                db = self._database()
                runtime = "ACTIVE" if db["orchestrator_running"] else "UNKNOWN"
                state = {**authority, "components": {
                    "orchestrator": {"status": runtime, "configured_mode": authority["orchestrator_mode"]},
                    "postgresql": {"status": "ACTIVE", "schema_version": SCHEMA_VERSION},
                    "signal_authority": {"status": "ACTIVE" if authority["signal_authority_mode"] == "DB_PRIMARY" else "DEGRADED"},
                    "jetstream": {"status": "UNKNOWN", "reason": "health is not asserted by the Control API"},
                    "trade_manager": {"status": "UNKNOWN", "reason": "no current canonical runtime instance"},
                    "execution": {"status": "INACTIVE" if authority["execution_authority_mode"] == "DISABLED" else "UNKNOWN"}},
                    "canonical_outbox_event_count": db["outbox_count"], "canonical_inbox_event_count": db["inbox_count"]}
                return 200, self._body(state, source="canonical_platform")
            if path == "/api/v1/safety":
                authority = self._authority(self.environ)
                self._database()  # Canonical-source reachability/schema is required to make this assertion.
                safe = (authority == {"orchestrator_mode": "PRIMARY", "signal_authority_mode": "DB_PRIMARY",
                                     "execution_authority_mode": "DISABLED"}
                        and self.environ.get("SIGNAL_DB_PRIMARY_ENABLED", "").lower() == "true")
                if not safe:
                    data = {**authority, "status": "BLOCKED", "execution_enabled": False,
                            "real_execution_mode_active": False, "broker_write_path_active": False,
                            "blockers": ["CANONICAL_AUTHORITY_CONFIGURATION_NOT_CONFIRMED"]}
                    return 200, self._body(data, source="canonical_platform", status="DEGRADED")
                data = {**authority, "status": "SAFE", "execution_enabled": False,
                        "real_execution": {"armed": False, "mode": "DISABLED"},
                        "canonical_order_send_gate": {"effective": "DISABLED"},
                        "execution_consumer": {"status": "INACTIVE"},
                        "real_execution_mode_active": False, "broker_write_path_active": False,
                        "blockers": []}
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
            broker_paths = {"/api/v1/broker/account", "/api/v1/broker/positions", "/api/v1/broker/pending-orders",
                            "/api/v1/broker/history-orders", "/api/v1/broker/deals", "/api/v1/broker/symbols",
                            "/api/v1/broker/exposure", "/api/v1/exposure"}
            if path in broker_paths:
                return 503, self._body(None, source="mt5_bridge_read_only", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message="Read-only MT5 bridge is not reachable from the platform API")
            if path == "/api/v1/reports" or path.startswith("/api/v1/reports/"):
                return 503, self._body(None, source="canonical_platform_reports", status="UNAVAILABLE",
                                       error="SOURCE_UNAVAILABLE", message="No canonical report registry is available")
            if path.startswith("/api/v1/"):
                return 404, self._body(None, source="platform_api_router", status="UNAVAILABLE", error="RESOURCE_NOT_FOUND")
            return 404, self._body(None, source="platform_api_router", status="UNAVAILABLE", error="RESOURCE_NOT_FOUND")
        except CanonicalSourceUnavailable as exc:
            return 503, self._body(None, source="canonical_postgres", status="UNAVAILABLE",
                                   error="SOURCE_UNAVAILABLE", message=str(exc))
        except (ValueError, TypeError):
            return 400, self._body(None, source="canonical_platform", status="UNAVAILABLE", error="INVALID_QUERY")
