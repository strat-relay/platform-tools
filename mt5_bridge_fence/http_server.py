"""Real HTTP transport for `RealBridgeFenceBoundary` - stdlib `http.server` only (matching
`execution_v2/runtime/health.py` and `control_api/app.py`'s existing convention, no new
dependency). This is what the actual `mt5-native-bridge` process would expose once this code is
ported there; here it is used ONLY to prove the platform-side HTTP client
(`execution_v2/runtime/bridge_client.py::HttpBridgeFenceClient`) actually talks to the real
boundary code over a real socket, bound to an OS-assigned ephemeral loopback port - **never**
port 22348, and never started as part of any deployment artifact in this mission.

The `broker_call` this server's boundary uses for the final MT5 primitive is supplied by
whoever constructs the server (a fake/counting callable in every test in this mission - see
`tests/test_execution_v2_real_bridge.py`). Nothing here ever talks to a real broker.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from execution_v2.bridge_fence_errors import BridgeFenceError, InvalidSignature
from execution_v2.fence import FenceGrant, WriteAuthorization

from .boundary import RealBridgeFenceBoundary

_STATUS_FOR_ERROR = {"InvalidSignature": 401}  # everything else BridgeFenceError -> 409 Conflict


def _grant_from_json(body: dict[str, Any]) -> FenceGrant:
    return FenceGrant(resource=body["resource"], generation=body["generation"], holder=body["holder"],
                      ttl_ms=body["ttl_ms"], not_after=body["not_after"], key_id=body["key_id"], sig=body["sig"])


def _authorization_from_json(body: dict[str, Any]) -> WriteAuthorization:
    return WriteAuthorization(resource=body["resource"], generation=body["generation"],
                              attempt_id=body["attempt_id"], tool=body["tool"],
                              request_fingerprint=body["request_fingerprint"], scope_class=body["scope_class"],
                              exp=body["exp"], key_id=body["key_id"], sig=body["sig"])


def _authorization_from_headers(headers: Any) -> WriteAuthorization:
    values = {"resource": headers.get("X-Fence-Resource"),
              "generation": headers.get("X-Fence-Generation"),
              "attempt_id": headers.get("X-Fence-Attempt-Id"),
              "tool": headers.get("X-Fence-Tool"),
              "request_fingerprint": headers.get("X-Fence-Request-Fingerprint"),
              "scope_class": headers.get("X-Fence-Scope-Class"),
              "exp": headers.get("X-Fence-Exp"), "key_id": headers.get("X-Fence-Key-Id"),
              "sig": headers.get("X-Fence-Sig")}
    if any(value is None for value in values.values()):
        raise KeyError("missing signed fence header")
    values["generation"] = int(values["generation"])
    return WriteAuthorization(**values)


def _make_handler(boundary: RealBridgeFenceBoundary, broker_call: Callable[[], dict[str, Any]]) -> type:
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

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._respond(200, {"ok": True, "bridge_epoch": boundary.bridge_epoch})
                return
            if self.path.startswith("/ledger/"):
                attempt_id = self.path[len("/ledger/"):]
                entry = boundary.ledger_entry(attempt_id)
                if entry is None:
                    self._respond(404, {"error": "NotFound", "attempt_id": attempt_id})
                    return
                self._respond(200, {"attempt_id": entry.attempt_id, "state": entry.state,
                                    "broker_response": entry.broker_response})
                return
            self._respond(404, {"error": "NotFound"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = self._read_json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._respond(400, {"error": "MalformedRequest"})
                return

            if self.path == "/advance_fence":
                try:
                    grant = _grant_from_json(body)
                    result = boundary.advance_fence(grant)
                except BridgeFenceError as exc:
                    self._respond(_STATUS_FOR_ERROR.get(type(exc).__name__, 409),
                                 {"error": type(exc).__name__, "message": str(exc)})
                    return
                except (KeyError, TypeError) as exc:
                    self._respond(400, {"error": "MalformedRequest", "message": str(exc)})
                    return
                self._respond(200, {"accepted": result.accepted, "bridge_epoch": result.bridge_epoch,
                                    "generation": result.generation, "cancelled": result.cancelled})
                return

            if self.path == "/submit":
                try:
                    authorization = _authorization_from_json(body["authorization"])
                    fingerprint = body["request_fingerprint"]
                    result = boundary.submit(authorization=authorization, request_fingerprint=fingerprint,
                                             broker_call=broker_call)
                except BridgeFenceError as exc:
                    self._respond(_STATUS_FOR_ERROR.get(type(exc).__name__, 409),
                                 {"error": type(exc).__name__, "message": str(exc)})
                    return
                except (KeyError, TypeError) as exc:
                    self._respond(400, {"error": "MalformedRequest", "message": str(exc)})
                    return
                self._respond(200, {"attempt_id": result.attempt_id, "state": result.state,
                                    "broker_response": result.broker_response})
                return

            if self.path == "/mcp":
                try:
                    method = body.get("method")
                    params = body.get("params") or {}
                    name = params.get("name")
                    if method != "tools/call" or not name:
                        raise ValueError("only JSON-RPC tools/call is supported")
                    if name in {"mt5_history", "mt5_orders", "mt5_positions"}:
                        payload = []
                    elif name == "mt5_canonical_order_send":
                        authorization = _authorization_from_headers(self.headers)
                        fingerprint = self.headers.get("X-Fence-Request-Fingerprint")
                        result = boundary.submit(authorization=authorization,
                                                 request_fingerprint=fingerprint or "",
                                                 broker_call=broker_call,
                                                 request_args=params.get("arguments") or {})
                        payload = {"id": result.broker_response.get("request_id") if result.broker_response else None,
                                   "state": result.state, "broker_response": result.broker_response}
                    else:
                        raise ValueError(f"unsupported tool {name}")
                    self._respond(200, {"jsonrpc": "2.0", "id": body.get("id"),
                                        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}})
                except BridgeFenceError as exc:
                    self._respond(200, {"jsonrpc": "2.0", "id": body.get("id"),
                                        "result": {"isError": True, "content": [{"type": "text",
                                        "text": f"{type(exc).__name__}: {exc}"}]}})
                except (KeyError, TypeError, ValueError) as exc:
                    self._respond(400, {"error": "MalformedRequest", "message": str(exc)})
                return

            self._respond(404, {"error": "NotFound"})

    return Handler


def start_bridge_fence_server(boundary: RealBridgeFenceBoundary, *, broker_call: Callable[[], dict[str, Any]],
                              bind_host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    """Deliberately single-threaded (`HTTPServer`, not `ThreadingHTTPServer`): the SQLite
    connection `RealBridgeFenceBoundary`/`FenceStore` holds is thread-affine, and - far more
    importantly - fence-generation and idempotency-ledger state is exactly the state a race
    between two concurrent requests must never be allowed to corrupt. Sequential request
    handling removes that entire class of bug rather than adding locking around it; this
    single-personal-account, low-volume execution slice has no throughput requirement that would
    justify the added risk.

    `port=0` (the default) asks the OS for an unused ephemeral port - the caller reads the
    actual bound port back via `server.server_address[1]`. This guarantees the test/proof server
    can never collide with, or be confused for, the live order-submission port this module's own
    docstring names."""
    server = HTTPServer((bind_host, port), _make_handler(boundary, broker_call))
    return server
