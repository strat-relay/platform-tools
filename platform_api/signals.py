from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from postgres.db import connect
from outcome_attribution import THEORETICAL_OUTCOME_DISCLAIMER


SCHEMA_VERSION = "012"
OUTCOME_SCHEMA_VERSION = "015"
EXECUTION_AUDIT_SCHEMA_VERSION = "021"
# Keep the implicit page small enough for the public Console request deadline. Larger pages
# remain available explicitly through pagination, but the default must not stream hundreds of
# kilobytes through the tunnel for every refresh.
DEFAULT_LIMIT = 10
MAX_LIMIT = 500

_SELECT = """
SELECT s.signal_id, s.candidate_id, s.evaluation_id, s.strategy_ref,
       s.strategy_id, s.strategy_version, s.parameter_set_ref,
       s.parameter_set_status, s.strategy_instance_id, s.instrument,
       s.direction, s.decision_time, s.signal_emitted_at, s.ingested_at,
       s.entry_type, s.entry_price, s.stop_price, s.risk_distance,
       s.target_price, s.target_distance, s.target_r,
       s.economic_position_id, s.entry_opportunity_id, s.setup_id,
       s.source_event_id, s.source_id, s.source_offset, s.evidence_class,
       s.cutoff_id, s.source_provenance, s.evaluation_hash, s.trace_hash,
       s.terminal_state, s.strategy_metadata, s.entry_signal_hash,
       outcomes.outcome_type, outcomes.status AS outcome,
       outcomes.realized_r, outcomes.exit_timestamp,
       outcomes.source AS outcome_source,
       mechanisms.entry_mechanisms,
       publication.publish_status AS publication_state,
       publication.published_at
FROM strategy.entry_signals AS s
LEFT JOIN LATERAL (
    SELECT array_agg(m.mechanism ORDER BY m.position) AS entry_mechanisms
    FROM strategy.entry_signal_mechanisms AS m
    WHERE m.entry_signal_id = s.signal_id
) AS mechanisms ON TRUE
LEFT JOIN LATERAL (
    SELECT o.publish_status, o.published_at
    FROM platform.outbox_events AS o
    WHERE o.aggregate_type = 'signal'
      AND o.aggregate_id = s.signal_id
      AND o.event_type = 'signal.entry.created.v1'
    ORDER BY o.created_at DESC, o.event_id
    LIMIT 1
) AS publication ON TRUE
LEFT JOIN strategy.entry_signal_outcomes AS outcomes
    ON outcomes.signal_id = s.signal_id
"""

# Same FROM/JOIN/WHERE shape as `_SELECT`, projected to a single count - kept as its own
# constant (not derived from `_SELECT` by string surgery) so a future edit to `_SELECT` doesn't
# silently desync the two.
_COUNT_SELECT = """
SELECT COUNT(*)
FROM strategy.entry_signals AS s
LEFT JOIN strategy.entry_signal_outcomes AS outcomes
    ON outcomes.signal_id = s.signal_id
"""

# A signal is still "open" when no outcome has been recorded yet (outcomes.status IS NULL,
# the common case - most signals haven't been evaluated) or when it has been recorded as
# explicitly OPEN. Every other SignalOutcome (TARGET_HIT/STOPPED/CLOSED/INVALIDATED/NO_RETRACE)
# is terminal. Open signals sort first, then newest-decided-first within each group - open
# signals are what an operator needs to see without paging past resolved history to find them.
_ORDER_BY = (
    " ORDER BY (CASE WHEN outcomes.status IS NULL OR outcomes.status = 'OPEN' THEN 0 ELSE 1 END),"
    " s.decision_time DESC, s.signal_id ASC"
)


class CanonicalSourceUnavailable(RuntimeError):
    """The canonical PostgreSQL source could not serve a read."""


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _row_dict(cursor: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    names = [column.name if hasattr(column, "name") else column[0]
             for column in cursor.description]
    return dict(zip(names, row, strict=True))


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """Return canonical fields plus precise aliases used by the current Console."""
    result = dict(row)
    result["entry_mechanisms"] = list(result.get("entry_mechanisms") or [])
    # These aliases have direct canonical equivalents; no values are inferred.
    result["symbol"] = result.get("instrument")
    result["signal_timestamp"] = result.get("decision_time")
    result["market_event_id"] = result.get("source_event_id")
    result.setdefault("outcome", None)
    result.setdefault("outcome_type", None)
    result.setdefault("realized_r", None)
    result.setdefault("exit_timestamp", None)
    result.setdefault("outcome_source", None)
    result.setdefault("executionSummary", [])
    result.setdefault("executionEvaluations", [])
    result["theoretical_outcome_disclaimer"] = THEORETICAL_OUTCOME_DISCLAIMER
    return result


class CanonicalSignalRepository:
    """Read EntrySignal rows only; every request is a PostgreSQL read-only txn."""

    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    @staticmethod
    def _check_schema(cursor: Any) -> None:
        cursor.execute("SET TRANSACTION READ ONLY")
        for version in (SCHEMA_VERSION, OUTCOME_SCHEMA_VERSION):
            cursor.execute(
                "SELECT version FROM platform.schema_migrations WHERE version = %s",
                (version,),
            )
            if cursor.fetchone() is None:
                raise CanonicalSourceUnavailable(
                    f"canonical PostgreSQL requires schema {version}"
                )

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cursor:
                    self._check_schema(cursor)
                    cursor.execute(sql, params)
                    return [_project(_row_dict(cursor, row)) for row in cursor.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL signal source unavailable") from exc

    def _count(self, sql: str, params: tuple[Any, ...]) -> int:
        """Same WHERE clause as `_query`, projected down to a single row count - used so the
        Console's pagination controls can show "page N of M" / "X-Y of Z", not just has_more."""
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cursor:
                    self._check_schema(cursor)
                    cursor.execute(sql, params)
                    row = cursor.fetchone()
                    return int(row[0]) if row else 0
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL signal source unavailable") from exc

    @staticmethod
    def _execution_evaluation(row: dict[str, Any], *, full: bool) -> dict[str, Any]:
        reason = row.get("block_reason") or row.get("result_outcome") or "PENDING"
        if row.get("result_outcome") == "FILLED":
            decision = "EXECUTED"
            reason = "FILLED"
        elif row.get("result_outcome") == "ACCEPTED":
            decision = "BROKER_ACCEPTED"
            reason = "ACCEPTED"
        elif row.get("result_outcome") in {"SUBMITTED", "UNKNOWN_RECONCILIATION_REQUIRED"}:
            decision = "RECONCILIATION_REQUIRED"
            reason = "BROKER_UNCONFIRMED"
        elif row.get("result_outcome") == "REJECTED":
            decision = "REJECTED"
        elif row.get("result_outcome") == "BLOCKED" or row.get("intent_status") == "BLOCKED":
            skipped = {"EXECUTION_AUTHORITY_DISABLED", "ACCOUNT_NOT_ALLOWED", "STRATEGY_NOT_ALLOWED",
                       "SYMBOL_NOT_ALLOWED"}
            decision = "SKIPPED" if reason in skipped else "REJECTED"
        elif reason in {"RISK_STATE_UNAVAILABLE", "BROKER_METADATA_UNAVAILABLE"}:
            decision = "ERROR"
        elif row.get("intent_status") in {"PENDING", "CLAIMED"}:
            decision = "PENDING"
        else:
            decision = "ERROR"
        human = {
            "EXECUTION_AUTHORITY_DISABLED": "Execution authority disabled",
            "MINIMUM_LOT_EXCEEDS_RISK_LIMIT": "Minimum lot exceeds risk limit",
            "RISK_STATE_UNAVAILABLE": "Risk state unavailable",
            "BROKER_METADATA_UNAVAILABLE": "Broker metadata unavailable",
            "ACCOUNT_NOT_ALLOWED": "Account outside policy scope",
            "STRATEGY_NOT_ALLOWED": "Strategy outside policy scope",
            "SYMBOL_NOT_ALLOWED": "Symbol outside policy scope",
            "MAX_ACCOUNT_EXPOSURE_EXCEEDED": "Account exposure limit exceeded",
            "MAX_CONCURRENT_POSITIONS_EXCEEDED": "Concurrent position limit exceeded",
            "MAX_CONCURRENT_ORDERS_EXCEEDED": "Concurrent order limit exceeded",
        }.get(reason, str(reason).replace("_", " ").title())
        account = str(row.get("account_id") or "")
        evaluation: dict[str, Any] = {
            "account": ("•" * max(0, len(account) - 4) + account[-4:]) if account else None,
            "decision": decision,
            "reason": reason,
            "humanReason": human,
        }
        if full:
            if row.get("risk_policy_version") is not None or row.get("policy_fingerprint"):
                evaluation["policy"] = {"version": row.get("risk_policy_version"),
                                         "fingerprint": row.get("policy_fingerprint")}
            evidence_fields = ("risk_per_trade", "account_equity", "risk_budget_usd", "stop_distance",
                               "broker_volume_min", "broker_volume_step", "broker_volume_max",
                               "calculated_volume", "submitted_volume", "estimated_loss_usd",
                               "daily_loss_used", "concurrent_positions_used", "concurrent_orders_used",
                               "signal_age_seconds", "max_signal_age_seconds", "canary_consumed", "canary_max")
            risk = {key: row.get(key) for key in evidence_fields if row.get(key) is not None}
            diagnostics = row.get("risk_diagnostics") or {}
            if isinstance(diagnostics, str):
                try:
                    diagnostics = json.loads(diagnostics)
                except json.JSONDecodeError:
                    diagnostics = {}
            if isinstance(diagnostics, dict):
                # Keep the durable dollar arithmetic visible in the public signal detail.
                risk.update({key: value for key, value in diagnostics.items()
                             if key.endswith("_usd") or key == "exposure_check"})
            if risk:
                evaluation["riskEvaluation"] = risk
            account_state = {key: row.get(key) for key in ("account_equity", "daily_loss_used",
                             "concurrent_positions_used", "concurrent_orders_used") if row.get(key) is not None}
            if isinstance(diagnostics, dict):
                account_state.update({key: diagnostics[key] for key in
                                      ("account_exposure_usd", "max_account_exposure_usd",
                                       "remaining_account_exposure_usd") if diagnostics.get(key) is not None})
            if account_state:
                evaluation["accountState"] = account_state
            sizing = {key: row.get(key) for key in ("calculated_volume", "submitted_volume",
                      "broker_volume_min", "broker_volume_step", "broker_volume_max",
                      "estimated_loss_usd") if row.get(key) is not None}
            if sizing:
                evaluation["sizing"] = sizing
            execution = {"intentId": row.get("execution_intent_id"), "status": row.get("intent_status")}
            if row.get("attempt_id"):
                execution.update({"attemptId": row.get("attempt_id"), "attemptState": row.get("attempt_state")})
            if any(value is not None for value in execution.values()):
                evaluation["execution"] = execution
            broker = {key: row.get(key) for key in ("broker_order_id", "broker_deal_id", "broker_position_id",
                                                     "result_outcome")
                      if row.get(key) is not None}
            if broker:
                evaluation["brokerResult"] = broker
        return evaluation

    def _execution_audit(self, signal_ids: list[str], *, full: bool) -> dict[str, list[dict[str, Any]]]:
        if not signal_ids:
            return {}
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s",
                                (EXECUTION_AUDIT_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL requires schema 021")
                    cur.execute("SELECT value FROM platform.system_metadata WHERE key = %s",
                                ("execution.audit_cutoff",))
                    cutoff_row = cur.fetchone()
                    cutoff_value = cutoff_row[0] if cutoff_row else {}
                    if isinstance(cutoff_value, str):
                        cutoff_value = json.loads(cutoff_value)
                    cutoff = cutoff_value.get("cutoff_utc") if isinstance(cutoff_value, dict) else None
                    cur.execute("""SELECT i.entry_signal_id, i.execution_intent_id, i.account_id,
                                      i.status AS intent_status, i.block_reason, i.risk_policy_version,
                                      e.policy_fingerprint, e.risk_per_trade, e.account_equity,
                                      e.risk_budget_usd, e.stop_distance, e.broker_volume_min,
                                      e.broker_volume_step, e.broker_volume_max, e.calculated_volume,
                                      e.submitted_volume, e.estimated_loss_usd, e.daily_loss_used,
                                      e.concurrent_positions_used, e.concurrent_orders_used,
                                      e.signal_age_seconds, e.max_signal_age_seconds,
                                      e.canary_consumed, e.canary_max, e.diagnostics AS risk_diagnostics,
                                      a.attempt_id, a.state AS attempt_state,
                                      r.outcome AS result_outcome, r.broker_order_id, r.broker_deal_id,
                                      r.broker_position_id
                               FROM execution_v2.execution_intent i
                               LEFT JOIN execution_v2.execution_risk_evidence e
                                 ON e.execution_intent_id = i.execution_intent_id
                               LEFT JOIN execution_v2.execution_attempt a
                                 ON a.execution_intent_id = i.execution_intent_id
                               LEFT JOIN execution_v2.execution_result r
                                 ON r.execution_intent_id = i.execution_intent_id
                               WHERE i.entry_signal_id = ANY(%s)
                               ORDER BY i.entry_signal_id, i.account_id""", (signal_ids,))
                    names = [column.name if hasattr(column, "name") else column[0] for column in cur.description]
                    rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
                    cur.execute("SELECT signal_id, decision_time FROM strategy.entry_signals WHERE signal_id = ANY(%s)",
                                (signal_ids,))
                    signal_times = {row[0]: row[1] for row in cur.fetchall()}
            by_signal: dict[str, list[dict[str, Any]]] = {signal_id: [] for signal_id in signal_ids}
            for row in rows:
                by_signal.setdefault(row["entry_signal_id"], []).append(self._execution_evaluation(row, full=full))
            for signal_id, decision_time in signal_times.items():
                if by_signal.get(signal_id):
                    continue
                before_cutoff = False
                if cutoff and decision_time:
                    try:
                        cutoff_dt = datetime.fromisoformat(str(cutoff).replace("Z", "+00:00"))
                        before_cutoff = decision_time < cutoff_dt
                    except (TypeError, ValueError):
                        before_cutoff = False
                by_signal[signal_id] = [{"account": None, "decision": "NOT_EVALUATED",
                                        "reason": "HISTORICAL_AUDIT_UNAVAILABLE" if before_cutoff else "NO_EXECUTION_INTENT",
                                        "humanReason": "Execution audit unavailable for this historical signal" if before_cutoff
                                        else "No V2 execution evaluation recorded"}]
            return by_signal
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL execution audit unavailable") from exc

    # Offset index for the append-only post-exit research ledger.
    # Stores byte offsets only (not records) so memory cost is O(n_signals) not O(file_size).
    # On lookup we seek to the stored offset and read one line.
    _post_exit_index: dict[str, int] = {}       # signal_id or trade_id → byte offset of latest line
    _post_exit_scanned: int = 0                 # byte offset scanned so far

    @classmethod
    def _refresh_post_exit_index(cls, path: Path) -> None:
        """Extend the offset index with any bytes appended since last scan."""
        try:
            current_size = path.stat().st_size
        except OSError:
            return
        if current_size <= cls._post_exit_scanned:
            return
        try:
            with path.open("rb") as f:
                f.seek(cls._post_exit_scanned)
                offset = cls._post_exit_scanned
                for raw_line in f:
                    stripped = raw_line.strip()
                    if stripped:
                        try:
                            record = json.loads(stripped)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            offset += len(raw_line)
                            continue
                        if sid := record.get("signal_id"):
                            cls._post_exit_index[sid] = offset
                        if tid := record.get("trade_id"):
                            cls._post_exit_index[tid] = offset
                    offset += len(raw_line)
            cls._post_exit_scanned = current_size
        except OSError:
            pass

    @classmethod
    def _post_exit_research(cls, row: dict[str, Any]) -> dict[str, Any] | None:
        """Read the separate Context research ledger without touching canonical signal state."""
        if row.get("strategy_id") != "CONTEXT_STRUCTURE_RETRACE_V1":
            return None
        path = Path(os.getenv("CONTEXT_POST_EXIT_RESEARCH_LEDGER", "/work/context_structure_retrace_post_exit.jsonl"))
        base = {"research_only": True, "source": "context_post_exit_ledger"}
        if not path.exists():
            return {**base, "status": "NOT_AVAILABLE", "reason": "research ledger is not mounted"}
        try:
            cls._refresh_post_exit_index(path)
            signal_id = row.get("signal_id")
            trade_id = row.get("economic_position_id")
            file_offset = cls._post_exit_index.get(signal_id) or cls._post_exit_index.get(trade_id)
            if file_offset is None:
                return {**base, "status": "NOT_OBSERVED", "reason": "no stopped-trade observation matches this signal"}
            with path.open("rb") as f:
                f.seek(file_offset)
                match = json.loads(f.readline())
            status = "DATA_GAP" if match.get("record_type") == "CONTEXT_STOPPED_POST_EXIT_DATA_GAP" else "AVAILABLE"
            return {**base, "status": status, "record": match}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {**base, "status": "SOURCE_UNAVAILABLE", "reason": f"{type(exc).__name__}: research ledger could not be read"}

    def list_signals(self, query: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            limit = int(query.get("limit", DEFAULT_LIMIT))
            offset = int(query.get("offset", 0))
        except ValueError as exc:
            raise ValueError("limit and offset must be integers") from exc
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        where: list[str] = []
        params: list[Any] = []
        filters = (
            (("strategy_id", "strategy"), "s.strategy_id = %s"),
            (("symbol", "instrument"), "s.instrument = %s"),
            (("direction",), "s.direction = %s"),
            (("evidence_class",), "s.evidence_class = %s"),
        )
        for names, clause in filters:
            value = next((query[name] for name in names if query.get(name)), None)
            if value is not None:
                where.append(clause)
                params.append(value)
        for name, operator in (("date_from", ">="), ("date_to", "<=")):
            value = query.get(name)
            if value:
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
                where.append(f"s.decision_time {operator} %s::timestamptz")
                params.append(value)
        search = query.get("search")
        if search:
            where.append("""(s.signal_id ILIKE %s OR s.instrument ILIKE %s
                         OR s.strategy_id ILIKE %s
                         OR COALESCE(s.economic_position_id, '') ILIKE %s
                         OR COALESCE(s.entry_opportunity_id, '') ILIKE %s)""")
            params.extend([f"%{search}%"] * 5)

        where_clause = (" WHERE " + " AND ".join(where)) if where else ""

        sql = _SELECT + where_clause + _ORDER_BY + " LIMIT %s OFFSET %s"
        rows = self._query(sql, tuple([*params, limit + 1, offset]))
        audit = self._execution_audit([row["signal_id"] for row in rows[:limit]], full=False)
        for row in rows[:limit]:
            row["executionSummary"] = audit.get(row["signal_id"], [])
        has_more = len(rows) > limit
        rows = rows[:limit]

        total = self._count(_COUNT_SELECT + where_clause, tuple(params))

        return rows, {"limit": limit, "offset": offset, "returned": len(rows),
                      "has_more": has_more, "total": total}

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        rows = self._query(_SELECT + " WHERE s.signal_id = %s", (signal_id,))
        audit = self._execution_audit([signal_id], full=True)
        if rows:
            rows[0]["executionEvaluations"] = audit.get(signal_id, [])
            rows[0]["postExitResearch"] = self._post_exit_research(rows[0])
        return rows[0] if rows else None


class PlatformSignalApi:
    def __init__(self, repository: CanonicalSignalRepository | None = None):
        self.repository = repository or CanonicalSignalRepository()

    @staticmethod
    def _envelope(data: Any = None, *, error: str | None = None,
                  message: str | None = None,
                  meta: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "api_version": "v1",
            "degraded": error is not None,
            "read_only": True,
            "source": "canonical_postgres",
        }
        if error:
            body["error"] = error
            if message:
                body["message"] = message
            body["unavailable"] = [{"code": error, "source": "canonical_postgres",
                                    "message": message or error}]
        else:
            body.update({"data": data, "unavailable": [], "schema_version": SCHEMA_VERSION,
                         "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
                         "execution_audit_schema_version": EXECUTION_AUDIT_SCHEMA_VERSION})
        if meta:
            body["meta"] = meta
        return body

    def execute(self, method: str, target: str) -> tuple[int, dict[str, Any]]:
        if method != "GET":
            return 405, self._envelope(error="READ_ONLY_API", message="GET only")
        from urllib.parse import parse_qs, unquote, urlsplit

        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query_values = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=32)
        # `_fresh` only varies the edge cache key (the Console bypasses cached copies right after
        # a write); it never affects the result.
        query = {key: values[-1] for key, values in query_values.items() if key != EDGE_CACHE_BYPASS_PARAM}
        if path == "/healthz":
            return 200, {"status": "ok", "service": "platform-signals-api"}
        if path == "/readyz":
            try:
                self.repository.list_signals({"limit": "1"})
            except CanonicalSourceUnavailable:
                return 503, {"status": "not_ready", "source": "canonical_postgres"}
            return 200, {"status": "ready", "source": "canonical_postgres",
                         "schema_version": SCHEMA_VERSION,
                         "outcome_schema_version": OUTCOME_SCHEMA_VERSION}
        if path == "/api/v1/signals":
            allowed = {"strategy_id", "strategy", "symbol", "instrument", "direction",
                       "search", "date_from", "date_to", "limit", "offset", "evidence_class"}
            unsupported = sorted(set(query) - allowed)
            if unsupported:
                return 400, self._envelope(error="UNSUPPORTED_QUERY_PARAMETER",
                                           message=", ".join(unsupported))
            try:
                rows, pagination = self.repository.list_signals(query)
            except ValueError as exc:
                return 400, self._envelope(error="INVALID_QUERY", message=str(exc))
            except CanonicalSourceUnavailable:
                return 503, self._envelope(error="SOURCE_UNAVAILABLE",
                                           message="canonical PostgreSQL signal source unavailable")
            return 200, self._envelope(rows, meta={"pagination": pagination})

        prefix = "/api/v1/signals/"
        if path.startswith(prefix) and path.count("/") == prefix.count("/"):
            signal_id = unquote(path[len(prefix):])
            if not signal_id:
                return 404, self._envelope(error="RESOURCE_NOT_FOUND", message=f"signal {signal_id!r} not found")
            try:
                row = self.repository.get_signal(signal_id)
            except CanonicalSourceUnavailable:
                return 503, self._envelope(error="SOURCE_UNAVAILABLE",
                                           message="canonical PostgreSQL signal source unavailable")
            if row is None:
                return 404, self._envelope(error="RESOURCE_NOT_FOUND", message=f"signal {signal_id!r} not found")
            return 200, self._envelope(row)
        return 404, self._envelope(error="RESOURCE_NOT_FOUND", message="route not found")


class UnifiedPlatformApi:
    """Dispatch canonical signal routes unchanged and all other API routes to Platform Control."""

    def __init__(self, signals: PlatformSignalApi | None = None, control_api: Any | None = None):
        from .control import PlatformControlApi
        self.signals = signals or PlatformSignalApi()
        self.control_api = control_api or PlatformControlApi()

    def execute(self, method: str, target: str, body: bytes | None = None) -> tuple[int, dict[str, Any]]:
        path = urlsplit(target).path.rstrip("/") or "/"
        if path == "/api/v1/signals" or path.startswith("/api/v1/signals/"):
            return self.signals.execute(method, target)
        return self.control_api.execute(method, target, body)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, default=_json_value, separators=(",", ":")).encode("utf-8")


# Edge (Cloudflare) caching for read models that tolerate a few seconds of staleness. The public
# path to this API runs through a Cloudflare tunnel; when it stalls, the edge serves the last good
# response (stale-if-error) instead of timing out. Only successful GETs of strategy and signal
# read models are cacheable; everything else - broker/account state, execution, Trade Manager,
# errors - stays no-store.
EDGE_CACHEABLE_PREFIXES = ("/api/v1/strategies", "/api/v1/signals")
EDGE_CACHE_CONTROL = "public, max-age=5, stale-while-revalidate=10, stale-if-error=300"
EDGE_CACHE_BYPASS_PARAM = "_fresh"


def edge_cache_control(method: str, target: str, status: int) -> str:
    path = urlsplit(target).path.rstrip("/") or "/"
    if method == "GET" and status == 200 and any(path == p or path.startswith(p + "/") for p in EDGE_CACHEABLE_PREFIXES):
        return EDGE_CACHE_CONTROL
    return "no-store"


def create_server(host: str = "0.0.0.0", port: int = 22350,
                  api: PlatformSignalApi | None = None,
                  allowed_origins: set[str] | frozenset[str] | None = None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import os
    from urllib.parse import urlsplit

    instance = api or UnifiedPlatformApi()
    origins = allowed_origins
    if origins is None:
        configured = os.getenv("PLATFORM_API_CORS_ORIGINS", "https://console.stratrelay.app")
        origins = frozenset(value.strip() for value in configured.split(",") if value.strip())

    def cors_headers(origin: str | None) -> list[tuple[str, str]]:
        # Exact-origin allowlist: do not reflect arbitrary Origin values or allow credentials.
        headers = [("Vary", "Origin")]
        if origin and origin in origins:
            headers.append(("Access-Control-Allow-Origin", origin))
        return headers

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any] | None = None,
                  *, extra_headers: list[tuple[str, str]] | None = None, cache_control: str = "no-store") -> None:
            encoded = _json_bytes(body) if body is not None else b""
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", cache_control)
            else:
                self.send_header("Content-Length", "0")
            for name, value in cors_headers(self.headers.get("Origin")):
                self.send_header(name, value)
            for name, value in extra_headers or []:
                self.send_header(name, value)
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)

        def do_GET(self) -> None:
            status, body = instance.execute("GET", self.path)
            self._send(status, body, cache_control=edge_cache_control("GET", self.path, status))

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            # Bounded read: never trust a client-supplied Content-Length to buffer unlimited
            # bytes into memory - platform_api/v2_risk.py separately enforces its own body size
            # limit on top of this.
            raw_body = self.rfile.read(min(length, 1_048_576)) if length > 0 else None
            status, body = instance.execute("POST", self.path, raw_body)
            self._send(status, body)

        # Paths that accept POST in addition to GET - kept as an explicit, narrow allowlist here
        # too so a browser's CORS preflight never promises more than the actual route dispatch
        # (PlatformControlApi.execute) is willing to accept.
        _POST_ALLOWED_PATHS = frozenset({"/api/v1/v2-execution/risk-policy",
                                         "/api/v1/v2-execution/authority",
                                         "/api/v1/v2-execution/canary-windows",
                                         "/api/v1/trade-manager/mode"})

        def do_OPTIONS(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            signal_detail_prefix = "/api/v1/signals/"
            if not path.startswith("/api/v1/"):
                self._send(404, {"error": "RESOURCE_NOT_FOUND", "message": "route not found"})
                return
            origin = self.headers.get("Origin")
            if not origin or origin not in origins:
                self._send(403, {"error": "CORS_ORIGIN_DENIED"})
                return
            from .control import PlatformControlApi  # local: control imports this module
            post_allowed = (path in self._POST_ALLOWED_PATHS
                            or PlatformControlApi._instance_lifecycle_route(path) is not None
                            or PlatformControlApi._strategy_write_route(path) is not None)
            allowed_methods = {"GET", "OPTIONS"} | ({"POST"} if post_allowed else set())
            requested_method = self.headers.get("Access-Control-Request-Method", "GET").upper()
            if requested_method not in allowed_methods:
                self._send(403, {"error": "CORS_METHOD_DENIED"})
                return
            requested_headers = {
                value.strip().lower()
                for value in self.headers.get("Access-Control-Request-Headers", "").split(",")
                if value.strip()
            }
            allowed_headers = {"authorization", "content-type"}
            if not requested_headers <= allowed_headers:
                self._send(403, {"error": "CORS_HEADERS_DENIED"})
                return
            self._send(204, extra_headers=[
                ("Access-Control-Allow-Methods", ", ".join(sorted(allowed_methods))),
                ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
                ("Access-Control-Max-Age", "600"),
            ])

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def address_string(self) -> str:
            # Skip reverse DNS lookup — default gethostbyaddr() adds ~13s latency per request
            # when no PTR record exists (common in k8s pod networks).
            return self.client_address[0]

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    import os

    server = create_server(os.getenv("PLATFORM_API_HOST", "0.0.0.0"),
                           int(os.getenv("PLATFORM_API_PORT", "22350")),
                           api=UnifiedPlatformApi())
    server.serve_forever()


if __name__ == "__main__":
    main()
