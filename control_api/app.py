from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
from platform_runtime import trading_platform_runtime_dir


ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "v1"
READ_ONLY_TOOLS = {
    "mt5_terminal_info", "mt5_account_info", "mt5_symbols", "mt5_symbol_info",
    "mt5_quote", "mt5_rates", "mt5_positions", "mt5_orders", "mt5_history",
}
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
QUERY_PARAMETERS = {
    "signals": {"strategy", "strategy_id", "symbol", "direction", "status", "environment", "component", "severity", "type", "event_type", "correlation_id", "failure_code", "search"},
    "executions": {"strategy", "symbol", "status", "environment"},
    "events": {"strategy", "strategy_id", "symbol", "direction", "status", "environment", "component", "severity", "type", "event_type", "correlation_id", "failure_code", "search"},
}


class SourceReadError(RuntimeError):
    def __init__(self, code: str, path: Path, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SourceReadError("SOURCE_UNAVAILABLE", path, "authoritative source is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise SourceReadError("SOURCE_UNAVAILABLE", path, "authoritative source could not be read")
    except json.JSONDecodeError:
        raise SourceReadError("SOURCE_MALFORMED", path, "authoritative JSON source is malformed")
    if not isinstance(value, dict):
        raise SourceReadError("SOURCE_MALFORMED", path, "authoritative JSON source must be an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SourceReadError("SOURCE_UNAVAILABLE", path, "authoritative source is unavailable")
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                    else:
                        raise SourceReadError("SOURCE_MALFORMED", path, "authoritative JSONL row must be an object")
                except json.JSONDecodeError:
                    raise SourceReadError("SOURCE_MALFORMED", path, "authoritative JSONL source is malformed")
    except OSError:
        raise SourceReadError("SOURCE_UNAVAILABLE", path, "authoritative source could not be read")
    return rows


class BridgeReader:
    def __init__(self, endpoint: str, timeout: float = 8.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        url = self.endpoint.rsplit("/mcp", 1)[0] + "/health"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in READ_ONLY_TOOLS:
            raise ValueError("control API permits only broker read tools")
        payload = {"jsonrpc": "2.0", "id": "control-api", "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}}
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Bridge-Origin": "CONTROL_API"},
            method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            outer = json.loads(response.read())
        result = outer.get("result", {})
        if result.get("isError"):
            raise RuntimeError((result.get("content") or [{}])[0].get("text", "broker read failed"))
        text = (result.get("content") or [{}])[0].get("text", "null")
        return json.loads(text)


class RuntimeSources:
    def __init__(self, root: Path = ROOT):
        self.root = root
        runtime = trading_platform_runtime_dir(root=root)
        self.orchestration = runtime / "orchestration"
        self.execution = runtime / "execution"

    def rows(self, stream: str) -> list[dict[str, Any]]:
        locations = {
            "signals": self.orchestration / "signals.jsonl",
            "events": self.orchestration / "events.jsonl",
            "audit": self.orchestration / "events.jsonl",
            "route_decisions": self.orchestration / "route_decisions.jsonl",
            "sizing_decisions": self.orchestration / "sizing_decisions.jsonl",
            "distribution_queue": self.orchestration / "distribution_queue.jsonl",
            "execution_intents": self.execution / "execution_intents.jsonl",
            "execution_decisions": self.execution / "execution_decisions.jsonl",
            "execution_skips": self.execution / "execution_skips.jsonl",
            "execution_events": self.execution / "events.jsonl",
            "account_snapshots": self.orchestration / "account_snapshots.jsonl",
        }
        return _read_jsonl(locations[stream]) if stream in locations else []

    def state(self, name: str) -> dict[str, Any]:
        paths = {
            "orchestration": self.orchestration / "state.json",
            "execution": self.execution / "state.json",
            "real": self.execution / "real_state.json",
            "resume": self.execution / "real_execution_resume.json",
            "manifest": self.orchestration / "manifest.json",
            "heartbeat": self.execution / "heartbeat.json",
            "build_manifest": self.root / "artifacts" / "MT5TradingBridge_execution_build_manifest.json",
        }
        return _read_json(paths[name])

    def strategy_cohort(self, strategy_id: str) -> dict[str, Any]:
        paths = {
            "CONTEXT_STRUCTURE_RETRACE_V1": self.root / "context_structure_retrace_forward_cohort.json",
        }
        path = paths.get(strategy_id)
        if path is None:
            return {}
        return _read_json(path)


class ControlApi:
    def __init__(self, root: Path = ROOT, bridge_factory=BridgeReader):
        self.sources = RuntimeSources(root)
        self.started_at = _now()
        self.config_issue: SourceReadError | None = None
        try:
            config = _read_json(root / "orchestration" / "config" / "platform.json")
        except SourceReadError as exc:
            config = {}
            self.config_issue = exc
        self.config = config
        self.data_endpoint = config.get("mcp_url", "http://127.0.0.1:22347/mcp")
        self.execution_endpoint = config.get("execution_mcp_url", "http://127.0.0.1:22348/mcp")
        self.bridge_factory = bridge_factory

    def _envelope(self, data: Any, *, degraded: bool = False, unavailable: list[Any] | None = None) -> dict[str, Any]:
        return {"api_version": API_VERSION, "data": data, "degraded": degraded,
                "unavailable": unavailable or [], "read_only": True}

    def _source_envelope(self, exc: SourceReadError, *, resource: str | None = None, identifier: str | None = None) -> tuple[int, dict[str, Any]]:
        reason = {"code": exc.code, "source": str(exc.path), "message": exc.message}
        body = {"error": exc.code, "resource": resource, "id": identifier, "degraded": True,
                "unavailable": [reason], "read_only": True}
        return 503, {key: value for key, value in body.items() if value is not None}

    def _canonical_execution_summary(self) -> tuple[int, dict[str, Any]]:
        """Read canonical V2 execution state only behind an explicit opt-in.

        The default route remains the existing legacy read path. This seam is read-only and does
        not restore legacy execution authority or infer capability from row counts.
        """
        try:
            from postgres.config import PostgresConfig
            from postgres.db import connect
            from control_api.execution_v2_source import read_execution_v2_summary
            config = PostgresConfig.from_env()
            config.require_explicit_target()
            conn = connect(config, readonly=True)
            try:
                mode = os.getenv("EXECUTION_AUTHORITY_MODE", "DISABLED").strip().upper()
                account_id = os.getenv("V2_EXECUTION_ACCOUNT_ID") or None
                return 200, self._envelope(read_execution_v2_summary(
                    conn, execution_authority_mode=mode, account_id=account_id))
            finally:
                conn.close()
        except Exception as exc:
            return 503, {"error": "CANONICAL_EXECUTION_SOURCE_UNAVAILABLE",
                         "message": str(exc), "degraded": True, "read_only": True}

    def _validate_query(self, resource: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]] | None:
        unsupported = next((key for key in query if key not in QUERY_PARAMETERS.get(resource, set())), None)
        if unsupported is not None:
            return 400, {"error": "UNSUPPORTED_QUERY_PARAMETER", "parameter": unsupported}
        return None

    @staticmethod
    def _public_row(row: dict[str, Any], resource: str) -> dict[str, Any]:
        if resource != "signals":
            return dict(row)
        result = dict(row)
        technical: dict[str, Any] = {}
        if "strategy_instance_id" in result:
            technical["legacy_source_instance_id"] = result.pop("strategy_instance_id")
        if "source_event_id" in result:
            technical["legacy_source_reference"] = result.pop("source_event_id")
        if technical:
            result["technical_metadata"] = technical
        return result

    def _rows(self, stream: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        rows = self.sources.rows(stream)
        exact = ("strategy", "strategy_id", "symbol", "direction", "status", "environment",
                 "component", "severity", "type", "event_type", "correlation_id", "failure_code")
        for key in exact:
            if query.get(key):
                value = query[key][0]
                rows = [row for row in rows if str(row.get(key, row.get("payload", {}).get(key, ""))) == value]
        if query.get("search"):
            term = query["search"][0].lower()
            rows = [row for row in rows if term in json.dumps(row, sort_keys=True).lower()]
        return rows

    def _broker(self, endpoint: str, tool: str, arguments: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        try:
            return self.bridge_factory(endpoint).call(tool, arguments), None
        except Exception as exc:
            return None, "SOURCE_UNAVAILABLE: MT5 read unavailable"

    def _active_execution_epoch(self) -> dict[str, Any] | None:
        """Read the current boundary; this never mutates or advances it."""
        try:
            resume = self.sources.state("resume")
        except SourceReadError:
            return None
        cutoff = resume.get("real_execution_resumed_at")
        generation = resume.get("real_execution_resume_generation", resume.get("generation"))
        if not cutoff or generation is None:
            return None
        parsed = _parse_timestamp(cutoff)
        if parsed is None:
            return None
        return {"generation": generation, "cutoff": cutoff, "cutoff_timestamp": parsed,
                "timezone": "America/Denver"}

    @staticmethod
    def _after_cutoff(row: dict[str, Any], cutoff: datetime, *fields: str) -> bool:
        # Activity is admitted by its persisted creation/event timestamp.  A
        # missing or malformed timestamp is never guessed into the new epoch.
        for field in fields:
            parsed = _parse_timestamp(row.get(field))
            if parsed is not None:
                return parsed > cutoff
        return False

    def _context_strategy_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        """Enrich only the explicitly reset Context strategy's operational card."""
        result = dict(row)
        epoch = self._active_execution_epoch()
        try:
            cohort = self.sources.strategy_cohort("CONTEXT_STRUCTURE_RETRACE_V1")
        except SourceReadError:
            cohort = {}

        if epoch is None:
            current_signals: list[dict[str, Any]] = []
            current_events: list[dict[str, Any]] = []
        else:
            signals = [signal for signal in self.sources.rows("signals")
                       if signal.get("strategy_id") == row.get("strategy_id") and
                       self._after_cutoff(signal, epoch["cutoff_timestamp"], "created_at")]
            events = [event for event in self.sources.rows("events")
                      if event.get("strategy_id") == row.get("strategy_id") and
                      self._after_cutoff(event, epoch["cutoff_timestamp"], "timestamp", "created_at")]
            current_signals = signals
            current_events = events

        event_times = [
            parsed for item in current_events + current_signals
            for parsed in [_parse_timestamp(item.get("timestamp", item.get("created_at")))]
            if parsed is not None
        ]
        last_event = max(event_times) if event_times else None
        active_symbols = list(cohort.get("active_symbols") or [])
        current_entry_opportunities = {item.get("entry_opportunity_id") for item in current_signals
                                       if item.get("entry_opportunity_id")}
        current_economic_positions = {item.get("economic_position_id") for item in current_signals
                                      if item.get("economic_position_id")}
        summary = {
            "generation": epoch["generation"] if epoch else None,
            "cutoff": epoch["cutoff"] if epoch else None,
            "cutoff_local": _format_local_time(epoch["cutoff_timestamp"]) if epoch else None,
            "signals": len(current_signals),
            "entry_opportunities": len(current_entry_opportunities),
            "economic_positions": len(current_economic_positions),
            "last_event": last_event.isoformat() if last_event else None,
            "last_event_local": _format_local_time(last_event) if last_event else None,
            "active_symbols": active_symbols,
            "status": cohort.get("status", "ACTIVE"),
            "lifecycle": "Forward Observation",
        }
        result["current_epoch"] = summary
        # These aliases make the read model usable by a simple card without
        # requiring the client to understand the nested representation.
        result["current_epoch_signals"] = summary["signals"]
        result["current_epoch_entry_opportunities"] = summary["entry_opportunities"]
        result["current_epoch_economic_positions"] = summary["economic_positions"]
        result["current_epoch_last_event"] = summary["last_event"]
        result["active_symbols"] = active_symbols
        return result

    def _strategy_rows(self) -> list[dict[str, Any]]:
        rows = self.config.get("strategies", [])
        return [self._context_strategy_summary(row) if row.get("strategy_id") == "CONTEXT_STRUCTURE_RETRACE_V1" else dict(row)
                for row in rows]

    def safety(self) -> dict[str, Any]:
        try:
            manifest = self.sources.state("manifest")
            orchestration = self.sources.state("orchestration")
            execution = self.sources.state("execution")
            real = self.sources.state("real")
            resume = self.sources.state("resume")
        except SourceReadError as exc:
            return self._source_envelope(exc, resource="safety")[1]
        try:
            build_manifest = self.sources.state("build_manifest")
        except SourceReadError:
            # Capability evidence is independently fail-closed below; retain
            # the normal safety payload so the dashboard can show UNKNOWN.
            build_manifest = {}
        platform_mode = self.config.get("execution_mode")
        contradictions: list[str] = []
        if platform_mode != manifest.get("mode"):
            contradictions.append("PLATFORM_CONFIG_MODE_DIFFERS_FROM_MANIFEST_MODE")
        if manifest.get("mode") != execution.get("mode"):
            contradictions.append("MANIFEST_MODE_DIFFERS_FROM_EXECUTION_STATE")
        if bool(manifest.get("live_execution_enabled")) != bool(orchestration.get("live_execution_enabled")):
            contradictions.append("MANIFEST_AND_ORCHESTRATION_LIVE_FLAGS_DIFFER")
        if bool(orchestration.get("live_execution_enabled")) != bool(real.get("armed")):
            contradictions.append("ORCHESTRATION_LIVE_FLAG_DIFFERS_FROM_REAL_ARM")
        if not resume.get("real_execution_resumed_at"):
            contradictions.append("REAL_RESUME_CUTOFF_MISSING")

        # The build manifest is the authoritative capability evidence.  The
        # orchestrator allowlist is deliberately not used here: it describes
        # the tools exposed to that process, not whether the loaded execution
        # bridge was built with the canonical order-send implementation.
        canonical_evidence = build_manifest.get("canonical_order_send_enabled")
        if isinstance(canonical_evidence, bool):
            canonical_capability = "ENABLED" if canonical_evidence else "DISABLED"
        else:
            canonical_capability = "UNKNOWN"

        blockers = list(contradictions)
        try:
            execution_health = self.bridge_factory(self.execution_endpoint).health()
        except Exception:
            execution_health = {"ok": False}
        if execution_health.get("ok") is not True:
            blockers.append("EXECUTION_BRIDGE_UNHEALTHY")
        if canonical_capability == "DISABLED":
            blockers.append("CANONICAL_ORDERSEND_DISABLED")
        elif canonical_capability == "UNKNOWN":
            blockers.append("CANONICAL_ORDERSEND_CAPABILITY_UNKNOWN")

        # Optional authoritative checks are fail-closed when a source
        # explicitly reports a bad state, while older runtime schemas remain
        # readable when they do not yet carry that field.
        account_context_id = real.get("account_context_id")
        if resume.get("account_context_id") and account_context_id and resume["account_context_id"] != account_context_id:
            blockers.append("ACCOUNT_CONTEXT_MISMATCH")
        if real.get("armed") is False:
            blockers.append("REAL_CONSUMER_NOT_ARMED")
        sole_write_owner = execution.get("sole_write_owner", real.get("sole_write_owner"))
        if sole_write_owner is False:
            blockers.append("REAL_CONSUMER_NOT_SOLE_WRITE_OWNER")
        generation = resume.get("generation")
        orchestration_generation = orchestration.get("live_execution_resume_generation")
        if generation is not None and orchestration_generation is not None and generation != orchestration_generation:
            blockers.append("GENERATION_MISMATCH")
        if execution.get("unknown_active_outcomes", 0) or real.get("unknown_active_outcomes", 0):
            blockers.append("UNKNOWN_ACTIVE_OUTCOME")
        if execution.get("unresolved_attempts", 0) or real.get("unresolved_attempts", 0):
            blockers.append("UNRESOLVED_ATTEMPTS")

        status = "BLOCKED" if blockers else "SAFE"
        try:
            audit_rows = self.sources.rows("execution_events")
        except SourceReadError:
            audit_rows = []
        return self._envelope({
            "status": status,
            "execution_enabled": bool(manifest.get("live_execution_enabled")),
            "real_execution": {"armed": bool(real.get("armed")), "mode": real.get("mode"),
                                "account_context_id": real.get("account_context_id")},
            "canonical_order_send_gate": {
                "capability": canonical_capability,
                "effective": canonical_capability,
                "evidence": {"source": "artifacts/MT5TradingBridge_execution_build_manifest.json",
                              "canonical_order_send_enabled": canonical_evidence,
                              "verification": build_manifest.get("verification"),
                              "binary_sha256": build_manifest.get("binary_sha256")},
            },
            "execution_consumer": {"status": execution.get("status"), "mode": execution.get("mode"),
                                    "pid": _read_text(self.sources.execution / "pid")},
            "consistency": {"contradictions": contradictions, "authorities": {
                "platform_config": "orchestration/config/platform.json",
                "manifest": "runtime/orchestration/manifest.json",
                "orchestration_state": "runtime/orchestration/state.json",
                "real_state": "runtime/execution/real_state.json",
                "resume": "runtime/execution/real_execution_resume.json"}},
            "resume_cutoff": resume,
            "generation": {"generation": resume.get("generation"),
                            "cutoff": resume.get("real_execution_resumed_at"),
                            "source": "runtime/execution/real_execution_resume.json"},
            "blockers": blockers,
            "last_safety_audit": audit_rows[-1] if audit_rows else None,
        })

    def execute(self, method: str, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        if method != "GET":
            return 405, {"error": "READ_ONLY_API", "message": "Only GET is supported", "read_only": True}
        parts = [part for part in urlparse(path).path.split("/") if part]
        if not parts or parts[0] != "api" or len(parts) < 2 or parts[1] != API_VERSION:
            return 404, {"error": "NOT_FOUND"}
        resource = parts[3] if len(parts) > 3 and parts[2] == "broker" else (parts[2] if len(parts) > 2 else "")
        identifier = parts[4] if len(parts) > 4 and parts[2] == "broker" else (parts[3] if len(parts) > 3 else None)
        sub_resource = parts[4] if len(parts) > 4 and parts[2] == "strategies" else None
        if identifier and not ID_RE.fullmatch(identifier):
            return 400, {"error": "INVALID_ID"}
        query_error = self._validate_query(resource, query)
        if query_error:
            return query_error
        if resource == "system":
            if self.config_issue:
                return self._source_envelope(self.config_issue, resource="system")
            return 200, self._envelope({"service": "trading-control-api", "api_version": API_VERSION,
                                        "started_at": self.started_at, "host": "127.0.0.1",
                                        "sources": {"orchestration": str(self.sources.orchestration),
                                                    "execution": str(self.sources.execution)}})
        if resource == "safety":
            return 200, self.safety()
        if resource == "strategies" and identifier and sub_resource == "report":
            from control_api import observability
            found, data = observability.get_strategy_report(identifier)
            return 200, self._envelope(data, degraded=not found, unavailable=[] if found else [data.get("message", "unavailable")])
        if resource == "strategies" and identifier and sub_resource == "instances":
            from control_api import observability
            return 200, self._envelope(observability.list_family_instances(identifier))
        if resource == "strategies" and identifier and sub_resource == "shadow":
            from control_api import observability
            return 200, self._envelope(observability.get_strategy_shadows(identifier))
        if resource == "strategies":
            if self.config_issue:
                return self._source_envelope(self.config_issue, resource="strategy", identifier=identifier)
            rows = self._strategy_rows()
            if identifier:
                rows = [row for row in rows if row.get("strategy_id") == identifier]
                if not rows:
                    return 404, {"error": "RESOURCE_NOT_FOUND", "resource": "strategy", "id": identifier}
                return 200, self._envelope(rows[0])
            return 200, self._envelope(rows)
        if resource in {"signals", "events", "audit"}:
            stream = "audit" if resource == "audit" else resource
            try:
                rows = self._rows(stream, query)
            except SourceReadError as exc:
                return self._source_envelope(exc, resource=resource, identifier=identifier)
            if identifier:
                key = "signal_id" if resource == "signals" else "event_id"
                rows = [row for row in rows if row.get(key) == identifier]
                if not rows:
                    return 404, {"error": "RESOURCE_NOT_FOUND", "resource": resource[:-1] if resource.endswith("s") else resource, "id": identifier}
                return 200, self._envelope(self._public_row(rows[0], resource))
            return 200, self._envelope([self._public_row(row, resource) for row in rows])
        if resource == "executions" and identifier == "metrics":
            if os.getenv("CONTROL_API_EXECUTION_SOURCE", "").strip().lower() == "canonical":
                return self._canonical_execution_summary()
            try:
                intents = self.sources.rows("execution_intents")
                decisions = self.sources.rows("execution_decisions")
                events = self.sources.rows("execution_events")
                trades = _read_jsonl(self.sources.execution / "real_trades.jsonl")
            except SourceReadError as exc:
                return self._source_envelope(exc, resource="executions", identifier="metrics")
            return 200, self._envelope({"execution_intents": len(intents),
                "broker_capable_requests_attempted": sum(1 for row in decisions if row.get("details", {}).get("broker_capable_requests_attempted")),
                "mt5_order_send_attempted": sum(1 for row in decisions if row.get("details", {}).get("mt5_order_send_attempted")) + sum(1 for row in events if row.get("mt5_order_send_attempted")),
                "broker_orders_accepted": sum(1 for row in decisions if row.get("decision") in {"REAL_SUBMITTED", "DEMO_SUBMITTED"}),
                "broker_fills_observed": len(trades),
                "rejected": sum(1 for row in decisions if "REJECT" in str(row.get("decision", ""))),
                "blocked": len(self.sources.rows("execution_skips"))})
        if resource == "executions":
            if os.getenv("CONTROL_API_EXECUTION_SOURCE", "").strip().lower() == "canonical":
                return self._canonical_execution_summary()
            try:
                rows = self.sources.rows("execution_intents") + self.sources.rows("execution_decisions") + self.sources.rows("execution_skips")
            except SourceReadError as exc:
                return self._source_envelope(exc, resource="execution", identifier=identifier)
            if identifier:
                rows = [row for row in rows if identifier in {row.get("execution_intent_id"), row.get("execution_decision_id"), row.get("signal_id")}]
                if not rows:
                    return 404, {"error": "RESOURCE_NOT_FOUND", "resource": "execution", "id": identifier}
                return 200, self._envelope(rows[0])
            rows = [row for row in rows if all(not query.get(k) or str(row.get(k, "")) == query[k][0] for k in ("strategy", "symbol", "status", "environment"))]
            return 200, self._envelope(rows)
        broker_map = {"account": ("mt5_account_info", None), "positions": ("mt5_positions", None),
                      "pending-orders": ("mt5_orders", None), "history-orders": ("mt5_history", {"limit": 500}),
                      "deals": ("mt5_history", {"limit": 500}), "symbols": ("mt5_symbols", None)}
        if resource in broker_map:
            value, error = self._broker(self.data_endpoint, *broker_map[resource])
            return 200, self._envelope(value, degraded=error is not None, unavailable=[error] if error else [])
        if resource == "exposure":
            value, error = self._broker(self.data_endpoint, "mt5_positions", None)
            return 200, self._envelope({"positions": value, "derived": False}, degraded=error is not None, unavailable=[error] if error else [])
        if resource == "connections":
            data: dict[str, Any] = {}
            unavailable: list[str] = []
            for name, endpoint in (("Data Channel", self.data_endpoint), ("Execution Channel", self.execution_endpoint)):
                try:
                    health = self.bridge_factory(endpoint).health()
                    lifecycle = health.get("lifecycle", {})
                    data[name.lower().replace(" ", "_")] = {"id": name.lower().replace(" ", "_"), "name": name,
                        "endpoint": endpoint, "status": "UP" if health.get("ok") else "DEGRADED",
                        "queue_depth": health.get("pending"), "active_waiters": lifecycle.get("active_waiters"),
                        "request_counts": lifecycle.get("counts"), "last_success": lifecycle.get("last_response_timestamp"),
                        "protocol": "MT5 bridge HTTP/JSON-RPC"}
                except Exception as exc:
                    unavailable.append(f"{name}: {type(exc).__name__}: {exc}")
                    data[name.lower().replace(" ", "_")] = {"id": name.lower().replace(" ", "_"), "name": name,
                        "endpoint": endpoint, "status": "UNKNOWN"}
            return 200, self._envelope(data, degraded=bool(unavailable), unavailable=unavailable)
        if resource == "reports":
            if identifier:
                return 503, {"error": "SOURCE_UNAVAILABLE", "resource": "report", "id": identifier,
                             "degraded": True, "unavailable": [{"code": "SOURCE_UNAVAILABLE", "source": "report_registry", "message": "no authoritative report registry exists"}], "read_only": True}
            return 200, self._envelope({"available": [], "note": "No report registry is persisted by the authoritative runtime"}, degraded=True, unavailable=["report_registry"])
        return 404, {"error": "NOT_FOUND"}


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _format_local_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(ZoneInfo("America/Denver")).strftime("%b %-d, %Y %-I:%M:%S %p %Z")


DEFAULT_CORS_ORIGINS = ("http://localhost:5173",)


def cors_origins_from_env(env: Any = None) -> tuple[str, ...]:
    """Comma-separated CONTROL_API_CORS_ORIGINS; defaults to the local console dev origin."""
    raw = (os.environ if env is None else env).get("CONTROL_API_CORS_ORIGINS", "")
    origins = tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())
    return origins or DEFAULT_CORS_ORIGINS


class _Handler(BaseHTTPRequestHandler):
    server_version = "TradingControlAPI/1"

    def _send_cors(self) -> None:
        # Echo the request origin only if allow-listed; otherwise advertise the
        # first configured origin so the browser rejects any other page.
        allowed = self.server.cors_origins
        origin = self.headers.get("Origin", "")
        matched = origin in allowed
        self.send_header("Access-Control-Allow-Origin", origin if matched else allowed[0])
        self.send_header("Vary", "Origin")
        if matched:
            self.send_header("Access-Control-Allow-Credentials", "true")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            status, body = 400, {"error": "MALFORMED_QUERY"}
        else:
            status, body = self.server.api.execute("GET", self.path, query)
        raw = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_cors()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        self._readonly_reject()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _readonly_reject(self) -> None:
        raw = json.dumps({"error": "READ_ONLY_API", "read_only": True}).encode()
        self.send_response(405)
        self.send_header("Allow", "GET")
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: Any) -> None:
        return


def create_server(host: str | None = None, port: int | None = None, api: ControlApi | None = None):
    bind_host = host if host is not None else os.environ.get("CONTROL_API_HOST", "127.0.0.1")
    bind_port = port if port is not None else int(os.environ.get("CONTROL_API_PORT", "22349"))
    server = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    server.api = api or ControlApi()
    server.cors_origins = cors_origins_from_env()
    return server


if __name__ == "__main__":
    create_server().serve_forever()
