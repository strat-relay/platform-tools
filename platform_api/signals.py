from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlsplit

from postgres.db import connect


SCHEMA_VERSION = "012"
OUTCOME_SCHEMA_VERSION = "015"
DEFAULT_LIMIT = 100
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
        has_more = len(rows) > limit
        rows = rows[:limit]

        total = self._count(_COUNT_SELECT + where_clause, tuple(params))

        return rows, {"limit": limit, "offset": offset, "returned": len(rows),
                      "has_more": has_more, "total": total}

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        rows = self._query(_SELECT + " WHERE s.signal_id = %s", (signal_id,))
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
                         "outcome_schema_version": OUTCOME_SCHEMA_VERSION})
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
        query = {key: values[-1] for key, values in query_values.items()}
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
                  *, extra_headers: list[tuple[str, str]] | None = None) -> None:
            encoded = _json_bytes(body) if body is not None else b""
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
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
            self._send(status, body)

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
        _POST_ALLOWED_PATHS = frozenset({"/api/v1/v2-execution/risk-policy"})

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
            allowed_methods = {"GET", "OPTIONS"} | ({"POST"} if path in self._POST_ALLOWED_PATHS else set())
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

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    import os

    server = create_server(os.getenv("PLATFORM_API_HOST", "0.0.0.0"),
                           int(os.getenv("PLATFORM_API_PORT", "22350")),
                           api=UnifiedPlatformApi())
    server.serve_forever()


if __name__ == "__main__":
    main()
