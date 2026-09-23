"""Production HTTP/JSON-RPC client for the real mt5-native-bridge `/mcp` endpoint.

The bridge performs generation validation and the durable idempotency claim inline with the
signed `tools/call` request; the old proof-server `/advance_fence` and `/submit` protocol is
test-only and is rejected by this client.

`submit()`'s `broker_call` parameter is accepted only for interface parity with
`execution_v2.worker.ExecutionWorker` (which always supplies one, per its own required-parameter
invariant) - it is INTENTIONALLY NEVER INVOKED here. Over a real network boundary the decision of
how to call MT5 belongs entirely to the bridge process (mission section 6's ownership split:
"BRIDGE owns... broker order submission"), not to whatever Python callable the platform process
happens to hold; only the bridge-side `mt5_bridge_fence.http_server` module ever calls a
`broker_call`, and only with whatever primitive it was started with (always a fake/counting one
in this mission - nothing here can reach port 22348 or send a live order).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from ..bridge_fence_errors import (BridgeFenceError, ExpiredAuthorization, ExpiredGrant,
                                   InvalidSignature, RequestFingerprintMismatch, StaleGeneration,
                                   WrongAccount)
from ..bridge_fence_types import AdvanceResult, SubmitResult
from ..fence import FenceGrant, WriteAuthorization

_ERROR_CLASSES = {cls.__name__: cls for cls in
                  (InvalidSignature, ExpiredGrant, ExpiredAuthorization, StaleGeneration,
                   WrongAccount, RequestFingerprintMismatch)}


class BridgeUnreachable(RuntimeError):
    """The configured bridge endpoint could not be reached at all (network/DNS/timeout/non-JSON
    response) - distinct from an independent fence rejection (a `BridgeFenceError` subclass),
    which means the bridge WAS reached and explicitly said no. Callers must treat this the same
    as any other inability to determine a safe outcome (mission section 4: "inability to
    determine broker result safely" is itself a fail-closed condition)."""


class HttpBridgeFenceClient:
    def __init__(self, *, base_url: str, execution_mode: str = "REAL_EXECUTION",
                 timeout_s: float = 10.0) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/mcp"):
            raise ValueError("base_url must be the real bridge /mcp endpoint")
        if execution_mode not in ("DEMO_EXECUTION", "REAL_EXECUTION", "REAL_SMOKE_TEST"):
            raise ValueError("execution_mode must be an explicit bridge execution mode")
        self.execution_mode = execution_mode
        self.timeout_s = timeout_s

    def advance_fence(self, grant: FenceGrant) -> AdvanceResult:
        # The real bridge has no separate grant endpoint.  It validates the generation from the
        # authorization header during the subsequent /mcp call.  Keep this method for the shared
        # worker protocol, but perform no network call and never claim broker authorization here.
        return AdvanceResult(accepted=True, bridge_epoch=0, generation=grant.generation, cancelled=[])

    def submit(self, *, authorization: WriteAuthorization, request_fingerprint: str,
              broker_call: Callable[[], dict[str, Any]],
              request_args: dict[str, Any] | None = None) -> SubmitResult:
        del broker_call
        if not request_args:
            raise ValueError("request_args are required for the real /mcp bridge")
        if authorization.request_fingerprint != request_fingerprint:
            raise RequestFingerprintMismatch("authorization is not bound to this exact request")
        headers = {"Content-Type": "application/json"}
        headers["X-Execution-Mode"] = self.execution_mode
        header_values = {
            "resource": "X-Fence-Resource", "generation": "X-Fence-Generation",
            "attempt_id": "X-Fence-Attempt-Id", "tool": "X-Fence-Tool",
            "request_fingerprint": "X-Fence-Request-Fingerprint", "scope_class": "X-Fence-Scope-Class",
            "exp": "X-Fence-Exp", "key_id": "X-Fence-Key-Id", "sig": "X-Fence-Sig",
        }
        auth = authorization.to_dict()
        for field, name in header_values.items():
            headers[name] = str(auth[field])
        # The signed authorization is bound to the exact request. Send the caller-supplied
        # fingerprint independently so the bridge can reject any mismatch before dispatch.
        headers["X-Fence-Request-Fingerprint"] = str(request_fingerprint)
        body = {"jsonrpc": "2.0", "id": authorization.attempt_id,
                "method": "tools/call", "params": {"name": authorization.tool,
                "arguments": dict(request_args)}}
        response = self._mcp(body, headers=headers)
        result = response.get("result", {})
        if result.get("isError"):
            text = result.get("content", [{}])[0].get("text", "bridge rejected request")
            self._raise_bridge_error(text)
        payload_text = result.get("content", [{}])[0].get("text", "{}")
        payload = json.loads(payload_text)
        if payload.get("idempotent_duplicate"):
            state = payload.get("state") or "UNCERTAIN_AFTER_RESTART"
            broker_response = payload.get("broker_response")
            return SubmitResult(authorization.attempt_id,
                                "DISPATCHED" if state == "COMPLETED" else state,
                                broker_response)
        broker_response = payload.get("broker_response")
        if broker_response is not None:
            return SubmitResult(authorization.attempt_id, "DISPATCHED", broker_response)
        return SubmitResult(authorization.attempt_id, payload.get("state") or "DISPATCHED",
                            {"status": "SUBMITTED", "request_id": payload.get("id") or payload.get("request_id"),
                             "correlation_token": request_args.get("comment")})

    def _mcp(self, body: dict[str, Any], *, headers: dict[str, str]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(self.base_url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            raise BridgeUnreachable(f"real bridge at {self.base_url} is unreachable: {exc}") from exc

    @staticmethod
    def _raise_bridge_error(message: str) -> None:
        name = str(message).split(":", 1)[0].strip()
        error_cls = _ERROR_CLASSES.get(name)
        if error_cls is not None:
            raise error_cls(message)
        raise BridgeUnreachable(f"real bridge rejected request: {message}")

    def ledger_entry(self, attempt_id: str) -> SubmitResult | None:
        # The real bridge intentionally exposes no proof-server ledger endpoint. Reconcile using
        # broker-visible evidence through the existing read-only MCP tools and the exact MT5
        # comment token carried by the order request.
        token = f"SRV2:{attempt_id}"
        for tool, arguments in (("mt5_history", {"limit": 500}),
                                ("mt5_orders", {}), ("mt5_positions", {})):
            payload = self._read_tool(tool, arguments)
            match = self._find_correlation(payload, token)
            if match is not None:
                response = dict(match)
                response.setdefault("status", "FILLED")
                response.setdefault("correlation_token", token)
                return SubmitResult(attempt_id, "DISPATCHED", response)
        return None

    def _read_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        response = self._mcp({"jsonrpc": "2.0", "id": f"reconcile-{tool}",
                              "method": "tools/call", "params": {"name": tool,
                              "arguments": arguments}},
                             headers={"Content-Type": "application/json",
                                     "X-Execution-Mode": self.execution_mode})
        result = response.get("result", {})
        if result.get("isError"):
            raise BridgeUnreachable(f"bridge read {tool} failed: {result}")
        text = result.get("content", [{}])[0].get("text", "null")
        return json.loads(text)

    @classmethod
    def _find_correlation(cls, value: Any, token: str) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if any(isinstance(v, str) and v == token for v in value.values()):
                return value
            for child in value.values():
                found = cls._find_correlation(child, token)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_correlation(child, token)
                if found is not None:
                    return found
        return None
