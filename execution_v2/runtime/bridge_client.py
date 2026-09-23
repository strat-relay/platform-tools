"""`HttpBridgeFenceClient`: the ONLY bridge-fence implementation production runtime wiring
(`execution_v2/runtime/service.py`) is allowed to construct (mission
`CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION` section 2). It never simulates, mocks, or
locally fabricates a result - every `advance_fence`/`submit`/`ledger_entry` call is a real HTTP
request to a configured bridge endpoint (`RuntimeConfig.bridge_fence_url`, which
`RuntimeConfig.from_env()` requires - there is no default, so a deployment with no real bridge
configured fails closed before this class is ever constructed).

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
    def __init__(self, *, base_url: str, timeout_s: float = 10.0) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BridgeUnreachable(f"bridge returned unparseable error response (HTTP {exc.code})") from exc
            error_name = payload.get("error")
            error_cls = _ERROR_CLASSES.get(error_name)
            if error_cls is not None:
                raise error_cls(payload.get("message", error_name))
            raise BridgeUnreachable(f"bridge returned unrecognized error {error_name!r} (HTTP {exc.code})")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise BridgeUnreachable(f"bridge at {self.base_url} is unreachable: {exc}") from exc

    def advance_fence(self, grant: FenceGrant) -> AdvanceResult:
        response = self._post("/advance_fence", grant.to_dict())
        return AdvanceResult(accepted=response["accepted"], bridge_epoch=response["bridge_epoch"],
                             generation=response["generation"], cancelled=response.get("cancelled", []))

    def submit(self, *, authorization: WriteAuthorization, request_fingerprint: str,
              broker_call: Callable[[], dict[str, Any]]) -> SubmitResult:
        # `broker_call` deliberately unused - see module docstring.
        response = self._post("/submit", {"authorization": authorization.to_dict(),
                                          "request_fingerprint": request_fingerprint})
        return SubmitResult(response["attempt_id"], response["state"], response.get("broker_response"))

    def ledger_entry(self, attempt_id: str) -> SubmitResult | None:
        request = urllib.request.Request(f"{self.base_url}/ledger/{attempt_id}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise BridgeUnreachable(f"bridge ledger lookup failed (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise BridgeUnreachable(f"bridge at {self.base_url} is unreachable: {exc}") from exc
        return SubmitResult(payload["attempt_id"], payload["state"], payload.get("broker_response"))
