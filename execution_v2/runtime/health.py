"""Minimal health/readiness HTTP surface for Kubernetes. Stdlib only, matching `control_api/
app.py` and `trade_management/runtime/health.py`'s existing `ThreadingHTTPServer` convention -
reproduced locally (not imported from trade_management) to keep this package independently
auditable (mission section 6).

`/healthz` (liveness): the process is alive.
`/readyz` (readiness): Postgres, NATS, and the entry-signal consumer have all finished starting.
`/executionz`: always-on, truthful statement of whether this deployment is currently capable of a
broker effect - `execution_authority_mode` and `broker_writes` are read directly from the running
config, never hard-coded, so an operator (or Codex's audit) never has to trust a comment.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthState:
    def __init__(self, *, execution_authority_mode: str, account_id: str) -> None:
        self._lock = threading.Lock()
        self._ready_components: dict[str, bool] = {"postgres": False, "nats": False, "entry_signal_consumer": False}
        self._detail: dict[str, Any] = {}
        self.execution_authority_mode = execution_authority_mode
        self.account_id = account_id

    def mark_ready(self, component: str, *, detail: Any = None) -> None:
        with self._lock:
            self._ready_components[component] = True
            if detail is not None:
                self._detail[component] = detail

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"components": dict(self._ready_components), "detail": dict(self._detail)}

    def is_ready(self) -> bool:
        with self._lock:
            return all(self._ready_components.values())

    def execution_status(self) -> dict[str, Any]:
        return {"execution_authority_mode": self.execution_authority_mode, "account_id": self.account_id,
               "broker_writes": self.execution_authority_mode == "ENABLED"}


def _make_handler(state: HealthState) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence stdlib per-request logging
            pass

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path == "/healthz":
                self._respond(200, {"status": "alive"})
                return
            if self.path == "/readyz":
                snapshot = state.snapshot()
                self._respond(200 if state.is_ready() else 503, {"status": "ready" if state.is_ready() else "not_ready", **snapshot})
                return
            if self.path == "/executionz":
                self._respond(200, state.execution_status())
                return
            self._respond(404, {"status": "not_found"})

    return Handler


def start_health_server(state: HealthState, *, port: int, bind_host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind_host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="execution-v2-health-server")
    thread.start()
    return server
