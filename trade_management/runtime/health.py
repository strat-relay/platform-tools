"""Minimal health/readiness HTTP surface for Kubernetes (mission section 6). Stdlib only,
matching `control_api/app.py`'s existing `ThreadingHTTPServer` convention - no new dependency.

`/healthz` (liveness): the process is alive and the supervisor loop is running.
`/readyz` (readiness): every required component (Postgres, NATS, the activation boundary, the
three consumers/loops) has finished starting. Kubernetes should not route anything meaningful to
this pod before `/readyz` is 200 - though nothing in this service accepts inbound traffic that
matters for correctness; this is for operator visibility and rollout gating.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready_components: dict[str, bool] = {
            "postgres": False, "nats": False, "activation_boundary": False,
            "open_consumer": False, "observation_loop": False, "tm_none_consumer": False,
        }
        self._detail: dict[str, Any] = {}

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
            self._respond(404, {"status": "not_found"})

    return Handler


def start_health_server(state: HealthState, *, port: int, bind_host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind_host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="p4-health-server")
    thread.start()
    return server
