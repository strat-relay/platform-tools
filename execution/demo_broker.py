"""The only broker-write boundary, locked to the verified demo account."""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from execution.demo import DEMO_CONTEXT, DEMO_SERVER, REAL_SERVER_PREFIX


class BrokerSubmissionRejected(RuntimeError):
    """The broker/EA returned an explicit negative submission result."""

    def __init__(self, response: dict):
        self.response = response
        code = response.get("retcode")
        description = response.get("retcode_description") or response.get("error") or "BROKER_WRITE_REJECTED"
        detail = f"BROKER_REJECTED:{code}:{description}" if code is not None else str(description)
        super().__init__(detail)


class DemoExecutionAdapter:
    """Submit only after all caller-side demo guards have passed.

    This adapter is intentionally not used by the current DRY_RUN process.
    The configured endpoint must be the dedicated execution endpoint, never the
    research bridge.
    """

    def __init__(self, endpoint: str, *, mode: str, armed_context: str | None,
                 verified_snapshot: dict, transport_verified: bool, smoke_test_id: str | None = None):
        if mode not in {"DEMO_EXECUTION", "REAL_EXECUTION", "REAL_SMOKE_TEST"}:
            raise RuntimeError("EXPLICIT_EXECUTION_MODE_REQUIRED")
        if mode != "REAL_SMOKE_TEST" and (endpoint.endswith(":22347/mcp") or ":22347/" in endpoint):
            raise RuntimeError("RESEARCH_BRIDGE_NOT_ALLOWED_FOR_EXECUTION")
        if not armed_context or verified_snapshot.get("account_context_id") != armed_context:
            raise RuntimeError("ACCOUNT_CONTEXT_NOT_ARMED")
        server = (verified_snapshot.get("raw") or {}).get("server")
        if mode == "DEMO_EXECUTION" and (armed_context != DEMO_CONTEXT or server != DEMO_SERVER):
            raise RuntimeError("DEMO_ACCOUNT_NOT_VERIFIED")
        if mode in {"REAL_EXECUTION", "REAL_SMOKE_TEST"} and (not str(armed_context).endswith("@" + str(server)) or not str(server).startswith(REAL_SERVER_PREFIX) or (verified_snapshot.get("raw") or {}).get("type") not in (2, "2")):
            raise RuntimeError("REAL_ACCOUNT_NOT_VERIFIED")
        if not transport_verified:
            raise RuntimeError("DEDICATED_EXECUTION_TRANSPORT_NOT_VERIFIED")
        self.endpoint = endpoint
        self.mode = mode
        self.smoke_test_id = smoke_test_id

    def submit_market_order(self, *, broker_symbol: str, side: str, volume: float,
                            stop_loss: float, take_profit: float, comment: str,
                            idempotency_key: str | None = None) -> dict:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "mt5_market_order", "arguments": {
                                  "symbol": broker_symbol, "side": side, "volume": volume,
                                  "stop_loss": stop_loss, "take_profit": take_profit,
                                  "confirm": True, "comment": comment,
                                  "idempotency_key": idempotency_key}}}).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Execution-Mode": self.mode,
            "X-Bridge-Origin": "REAL_EXECUTION",
            "X-Bridge-Origin-Pid": str(os.getpid()),
            "X-Bridge-Priority-Class": "EXECUTION_CRITICAL",
            "X-Bridge-Max-Age-Ms": "5000",
        }
        if self.mode == "REAL_SMOKE_TEST":
            if not self.smoke_test_id: raise RuntimeError("SMOKE_TEST_ID_REQUIRED")
            headers["X-Smoke-Test-ID"] = self.smoke_test_id
        request = Request(self.endpoint, data=payload, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            raise RuntimeError("SUBMISSION_ACK_UNCERTAIN_RECONCILE_REQUIRED") from exc
        result = outer.get("result", {})
        if result.get("isError"):
            raise RuntimeError(result.get("content", [{}])[0].get("text", "BROKER_WRITE_REJECTED"))
        broker_result = json.loads(result["content"][0]["text"])
        # A completed EA/MCP request is not the same as broker acceptance.
        # MT5 market-order success is explicit in the trade retcode; preserve
        # the complete response when the broker rejects it so callers can
        # classify the outcome without probing positions as proof of failure.
        retcode = broker_result.get("retcode")
        success_codes = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
        if broker_result.get("ok") is False or (retcode is not None and int(retcode) not in success_codes):
            raise BrokerSubmissionRejected(broker_result)
        return broker_result

    def submit_canonical_market_order(self, *, request: dict, canonical_request_text: str,
                                      request_fingerprint: str, idempotency_key: str | None = None) -> dict:
        """Submit one exact canonical request through the dedicated bridge.

        The EA currently hard-disables this operation. Keeping the method
        explicit prevents production MARKET execution from falling back to
        the legacy CTrade/mt5_market_order path.
        """
        required = ("action", "magic", "symbol", "volume", "price", "sl", "tp",
                    "deviation", "type", "type_filling", "type_time", "expiration", "comment")
        missing = [field for field in required if field not in request]
        if missing:
            raise RuntimeError("CANONICAL_REQUEST_MISSING_FIELD:" + ",".join(missing))
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "mt5_canonical_order_send", "arguments": {
                                  "schema_version": request.get("schema_version", 1),
                                  "action": request["action"], "magic": request["magic"],
                                  "symbol": request["symbol"], "volume": request["volume"],
                                  "price": request["price"], "sl": request["sl"], "tp": request["tp"],
                                  "deviation": request["deviation"], "type": request["type"],
                                  "type_filling": request["type_filling"], "type_time": request["type_time"],
                                  "expiration": request["expiration"], "comment": request["comment"],
                                  "canonical_request_text": canonical_request_text,
                                  "request_fingerprint": request_fingerprint,
                                  "idempotency_key": idempotency_key}}}).encode()
        headers = {"Content-Type": "application/json", "X-Execution-Mode": self.mode,
                   "X-Bridge-Origin": "REAL_EXECUTION", "X-Bridge-Origin-Pid": str(os.getpid()),
                   "X-Bridge-Priority-Class": "EXECUTION_CRITICAL", "X-Bridge-Max-Age-Ms": "5000"}
        if self.mode == "REAL_SMOKE_TEST":
            if not self.smoke_test_id:
                raise RuntimeError("SMOKE_TEST_ID_REQUIRED")
            headers["X-Smoke-Test-ID"] = self.smoke_test_id
        request_obj = Request(self.endpoint, data=payload, headers=headers)
        try:
            with urlopen(request_obj, timeout=10) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            raise RuntimeError("SUBMISSION_ACK_UNCERTAIN_RECONCILE_REQUIRED") from exc
        result = outer.get("result", {})
        if result.get("isError"):
            raise RuntimeError(result.get("content", [{}])[0].get("text", "CANONICAL_ORDER_SEND_REJECTED"))
        broker_result = json.loads(result["content"][0]["text"])
        retcode = broker_result.get("retcode")
        if broker_result.get("ok") is False or (retcode is not None and int(retcode) not in {10008, 10009, 10010}):
            raise BrokerSubmissionRejected(broker_result)
        return broker_result

    def close_position(self, *, ticket: int) -> dict:
        if self.mode not in {"REAL_SMOKE_TEST", "DEMO_EXECUTION"}:
            raise RuntimeError("EXPLICIT_SMOKE_OR_DEMO_MODE_REQUIRED")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "mt5_close_position", "arguments": {
                                  "ticket": int(ticket), "confirm": True}}}).encode()
        headers = {"Content-Type": "application/json", "X-Execution-Mode": self.mode,
                   "X-Smoke-Test-ID": self.smoke_test_id or ""}
        request = Request(self.endpoint, data=payload, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            raise RuntimeError("CLOSE_ACK_UNCERTAIN_RECONCILE_REQUIRED") from exc
        result = outer.get("result", {})
        if result.get("isError"):
            raise RuntimeError(result.get("content", [{}])[0].get("text", "BROKER_CLOSE_REJECTED"))
        return json.loads(result["content"][0]["text"])
