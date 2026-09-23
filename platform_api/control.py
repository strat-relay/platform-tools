"""Read-only platform-owned Control API routes.

This module intentionally has no filesystem runtime-state fallback.  Current
authority comes from the deployed authority environment, canonical PostgreSQL,
and (for the active strategy definition only) the mounted platform config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

SCHEMA_VERSION = "012"
TRADE_MANAGEMENT_SCHEMA_VERSION = "013"
TRADE_MANAGEMENT_SCHEMA_VERSION = "013"
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

    def _trade_management_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Read P4 tables without making an empty dataset look unavailable."""
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s",
                                (TRADE_MANAGEMENT_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable(
                            f"canonical PostgreSQL requires schema {TRADE_MANAGEMENT_SCHEMA_VERSION}")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL trade management source unavailable") from exc

    def trade_manager_summary(self) -> dict[str, Any]:
        rows = self._trade_management_query("""
            SELECT
              (SELECT count(*) FROM trade_management.managed_trade) AS total_managed_trades,
              (SELECT count(*) FROM trade_management.managed_trade WHERE state = 'OPEN') AS open_managed_trades,
              (SELECT max(observed_at) FROM trade_management.trade_observation) AS latest_observation_at,
              (SELECT max(decision_time) FROM trade_management.trade_manager_decision) AS latest_decision_at,
              (SELECT count(*) FROM trade_management.trade_observation) AS observation_count,
              (SELECT count(*) FROM trade_management.trade_manager_decision) AS decision_count,
              (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'PUBLISHED') AS published_decision_count,
              (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'WITHHELD') AS withheld_decision_count,
              (SELECT json_agg(v ORDER BY v.tm_version_id)
                 FROM (SELECT tm_version_id, evaluator_id, label, status
                         FROM trade_management.trade_manager_version) v) AS policy_versions
        """)
        return rows[0]

    def managed_trades(self, limit: int, offset: int, trade_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE mt.managed_trade_id = %s" if trade_id else ""
        params: tuple[Any, ...] = (trade_id,) if trade_id else (limit, offset)
        tail = "" if trade_id else " LIMIT %s OFFSET %s"
        return self._trade_management_query(f"""
            SELECT mt.managed_trade_id, mt.entry_signal_id, mt.strategy_id,
                   mt.strategy_version, mt.strategy_ref, mt.instrument, mt.direction,
                   mt.decision_time AS opened_at, mt.reference_entry_price AS entry,
                   mt.initial_stop, mt.initial_target AS target, mt.state,
                   mt.tm_version_id, mt.binding_resolution, mt.evidence_mode,
                   mt.eligibility, mt.eligibility_reason, mt.record_mode,
                   mt.last_observation_seq, mt.created_at,
                   tv.label AS policy_label,
                   obs.observation_id AS latest_observation_id,
                   obs.observation_seq AS latest_observation_seq,
                   obs.observed_at AS latest_observation_at,
                   obs.effective_at AS latest_observation_effective_at,
                   obs.data_status AS observation_data_status,
                   dec.decision_id AS latest_decision_id,
                   dec.action AS latest_decision,
                   dec.reason_codes AS latest_reason_codes,
                   dec.decision_time AS latest_decision_at,
                   dec.persisted_at AS latest_decision_persisted_at,
                   dec.data_status AS decision_data_status,
                   pub.outcome AS publication_outcome,
                   pub.reason AS publication_reason,
                   pub.evaluated_at AS publication_evaluated_at
            FROM trade_management.managed_trade mt
            LEFT JOIN trade_management.trade_manager_version tv ON tv.tm_version_id = mt.tm_version_id
            LEFT JOIN LATERAL (
              SELECT o.* FROM trade_management.trade_observation o
              WHERE o.managed_trade_id = mt.managed_trade_id
              ORDER BY o.observation_seq DESC LIMIT 1
            ) obs ON TRUE
            LEFT JOIN LATERAL (
              SELECT d.* FROM trade_management.trade_manager_decision d
              WHERE d.managed_trade_id = mt.managed_trade_id
              ORDER BY d.observation_seq DESC, d.persisted_at DESC LIMIT 1
            ) dec ON TRUE
            LEFT JOIN trade_management.publication_decision pub ON pub.decision_id = dec.decision_id
            {where}
            ORDER BY mt.created_at DESC, mt.managed_trade_id
            {tail}
        """, params)

    def trade_decisions(self, trade_id: str) -> list[dict[str, Any]]:
        return self._trade_management_query("""
            SELECT d.decision_id, d.managed_trade_id, d.observation_id,
                   d.observation_seq, d.action, d.parameters, d.reason_codes,
                   d.decision_trace_ref, d.decision_time, d.persisted_at,
                   d.data_status, d.record_mode, p.outcome AS publication_outcome,
                   p.reason AS publication_reason, p.evaluated_at AS publication_evaluated_at
            FROM trade_management.trade_manager_decision d
            LEFT JOIN trade_management.publication_decision p ON p.decision_id = d.decision_id
            WHERE d.managed_trade_id = %s
            ORDER BY d.observation_seq DESC, d.persisted_at DESC
        """, (trade_id,))

    def _trade_management_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Read the P4 observability tables in a read-only transaction.

        This deliberately has its own schema gate: an older database remains
        a source-unavailable response, while a migrated database with zero
        rows is a healthy empty state.
        """
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s",
                                (TRADE_MANAGEMENT_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable(
                            f"canonical PostgreSQL requires schema {TRADE_MANAGEMENT_SCHEMA_VERSION}")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL trade management source unavailable") from exc

    def trade_manager_summary(self) -> dict[str, Any]:
        rows = self._trade_management_query("""
            SELECT
              (SELECT count(*) FROM trade_management.managed_trade) AS total_managed_trades,
              (SELECT count(*) FROM trade_management.managed_trade WHERE state = 'OPEN') AS open_managed_trades,
              (SELECT max(observed_at) FROM trade_management.trade_observation) AS latest_observation_at,
              (SELECT max(decision_time) FROM trade_management.trade_manager_decision) AS latest_decision_at,
              (SELECT count(*) FROM trade_management.trade_observation) AS observation_count,
              (SELECT count(*) FROM trade_management.trade_manager_decision) AS decision_count,
              (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'PUBLISHED') AS published_decision_count,
              (SELECT count(*) FROM trade_management.publication_decision WHERE outcome = 'WITHHELD') AS withheld_decision_count,
              (SELECT json_agg(v ORDER BY v.tm_version_id)
                 FROM (SELECT tm_version_id, evaluator_id, label, status FROM trade_management.trade_manager_version) v
              ) AS policy_versions
        """)
        return rows[0]

    def managed_trades(self, limit: int, offset: int, trade_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE mt.managed_trade_id = %s" if trade_id else ""
        params: tuple[Any, ...] = (trade_id,) if trade_id else (limit, offset)
        tail = "" if trade_id else " LIMIT %s OFFSET %s"
        return self._trade_management_query(f"""
            SELECT mt.managed_trade_id, mt.entry_signal_id, mt.strategy_id,
                   mt.strategy_version, mt.strategy_ref, mt.instrument, mt.direction,
                   mt.decision_time AS opened_at, mt.reference_entry_price AS entry,
                   mt.initial_stop, mt.initial_target AS target, mt.state,
                   mt.tm_version_id, mt.binding_resolution, mt.evidence_mode,
                   mt.eligibility, mt.eligibility_reason, mt.record_mode,
                   mt.last_observation_seq, mt.created_at,
                   tv.label AS policy_label,
                   obs.observation_id AS latest_observation_id,
                   obs.observation_seq AS latest_observation_seq,
                   obs.observed_at AS latest_observation_at,
                   obs.effective_at AS latest_observation_effective_at,
                   obs.data_status AS observation_data_status,
                   dec.decision_id AS latest_decision_id,
                   dec.action AS latest_decision,
                   dec.reason_codes AS latest_reason_codes,
                   dec.decision_time AS latest_decision_at,
                   dec.persisted_at AS latest_decision_persisted_at,
                   dec.data_status AS decision_data_status,
                   pub.outcome AS publication_outcome,
                   pub.reason AS publication_reason,
                   pub.evaluated_at AS publication_evaluated_at
            FROM trade_management.managed_trade mt
            LEFT JOIN trade_management.trade_manager_version tv ON tv.tm_version_id = mt.tm_version_id
            LEFT JOIN LATERAL (
              SELECT o.* FROM trade_management.trade_observation o
              WHERE o.managed_trade_id = mt.managed_trade_id
              ORDER BY o.observation_seq DESC LIMIT 1
            ) obs ON TRUE
            LEFT JOIN LATERAL (
              SELECT d.* FROM trade_management.trade_manager_decision d
              WHERE d.managed_trade_id = mt.managed_trade_id
              ORDER BY d.observation_seq DESC, d.persisted_at DESC LIMIT 1
            ) dec ON TRUE
            LEFT JOIN trade_management.publication_decision pub ON pub.decision_id = dec.decision_id
            {where}
            ORDER BY mt.created_at DESC, mt.managed_trade_id
            {tail}
        """, params)

    def trade_decisions(self, trade_id: str) -> list[dict[str, Any]]:
        return self._trade_management_query("""
            SELECT d.decision_id, d.managed_trade_id, d.observation_id,
                   d.observation_seq, d.action, d.parameters, d.reason_codes,
                   d.decision_trace_ref, d.decision_time, d.persisted_at,
                   d.data_status, d.record_mode, p.outcome AS publication_outcome,
                   p.reason AS publication_reason, p.evaluated_at AS publication_evaluated_at
            FROM trade_management.trade_manager_decision d
            LEFT JOIN trade_management.publication_decision p ON p.decision_id = d.decision_id
            WHERE d.managed_trade_id = %s
            ORDER BY d.observation_seq DESC, d.persisted_at DESC
        """, (trade_id,))


class PlatformControlApi:
    def __init__(self, repository: PlatformControlRepository | None = None,
                 environ: dict[str, str] | None = None,
                 strategy_config_path: str | None = None):
        self.repository = repository or PlatformControlRepository()
        self.environ = os.environ if environ is None else environ
        self.strategy_config_path = strategy_config_path or self.environ.get(
            "PLATFORM_STRATEGY_CONFIG_PATH", "/etc/platform-config/platform.json")

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

    def execute(self, method: str, target: str) -> tuple[int, dict[str, Any]]:
        if method != "GET":
            return 405, self._body(None, source="platform", status="UNAVAILABLE", error="READ_ONLY_API", message="GET only")
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query_values = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=32)
        query = {k: v[-1] for k, v in query_values.items()}
        try:
            if path == "/healthz":
                return 200, {"status": "ok", "service": "platform-control-api"}
            if path == "/readyz":
                self._database()
                return 200, {"status": "ready", "service": "platform-control-api",
                             "source": "canonical_postgres", "schema_version": SCHEMA_VERSION}
            if path == "/api/v1/system":
                authority = self._authority(self.environ)
                db = self._database()
                try:
                    tm = self.repository.trade_manager_summary()
                    trade_manager = {
                        "status": "ACTIVE",
                        "source": "canonical_postgres",
                        "managed_trade_count": tm["total_managed_trades"],
                        "open_managed_trade_count": tm["open_managed_trades"],
                    }
                except CanonicalSourceUnavailable:
                    trade_manager = {"status": "UNKNOWN", "reason": "canonical Trade Manager observability unavailable"}
                runtime = "ACTIVE" if db["orchestrator_running"] else "UNKNOWN"
                state = {**authority, "components": {
                    "orchestrator": {"status": runtime, "configured_mode": authority["orchestrator_mode"]},
                    "postgresql": {"status": "ACTIVE", "schema_version": SCHEMA_VERSION},
                    "signal_authority": {"status": "ACTIVE" if authority["signal_authority_mode"] == "DB_PRIMARY" else "DEGRADED"},
                    "jetstream": {"status": "UNKNOWN", "reason": "health is not asserted by the Control API"},
                    "trade_manager": trade_manager,
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
                summary = self.repository.trade_manager_summary()
                return 200, self._body(summary, source="canonical_postgres")
            if path.startswith("/api/v1/managed-trades/") and path.endswith("/decisions"):
                trade_id = unquote(path[len("/api/v1/managed-trades/"):-len("/decisions")].strip("/"))
                rows = self.repository.trade_decisions(trade_id)
                return 200, self._body(rows, source="canonical_postgres")
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
