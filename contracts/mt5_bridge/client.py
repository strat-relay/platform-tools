"""Small, policy-free client for the frozen MT5 bridge protocol."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (BridgeAckUncertain, BridgeReadTimeout, BridgeTransportError,
                     classify_tool_error)

READ_TOOLS = frozenset({
    "mt5_terminal_info", "mt5_account_info", "mt5_symbols", "mt5_symbol_info",
    "mt5_quote", "mt5_rates", "mt5_rates_range", "mt5_symbol_snapshot",
    "mt5_positions", "mt5_orders", "mt5_history", "mt5_order_check",
})
WRITE_TOOLS = frozenset({
    "mt5_canonical_order_send", "mt5_market_order", "mt5_pending_order",
    "mt5_cancel_pending_order", "mt5_close_position", "mt5_trailing_stop",
})


@dataclass(frozen=True)
class BridgeEndpoint:
    url: str
    profile: str = "research"


@dataclass(frozen=True)
class CallContext:
    origin: str = "UNKNOWN_LEGACY"
    priority: str | None = None
    max_age_ms: int | None = None
    no_cache: bool = False
    execution_mode: str | None = None
    smoke_test_id: str | None = None


class Mt5ReadClient:
    def __init__(self, endpoint: BridgeEndpoint | str, *, timeout_s: float = 35):
        self.endpoint = endpoint if isinstance(endpoint, BridgeEndpoint) else BridgeEndpoint(endpoint)
        self.timeout_s = timeout_s

    def _headers(self, ctx: CallContext) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Origin": ctx.origin,
            "X-Bridge-Origin-Pid": str(os.getpid()),
        }
        if ctx.priority:
            headers["X-Bridge-Priority-Class"] = ctx.priority
        if ctx.max_age_ms is not None:
            headers["X-Bridge-Max-Age-Ms"] = str(ctx.max_age_ms)
        if ctx.no_cache:
            headers["X-Bridge-Cache-Control"] = "no-cache"
        if ctx.execution_mode:
            headers["X-Execution-Mode"] = ctx.execution_mode
        if ctx.smoke_test_id:
            headers["X-Smoke-Test-ID"] = ctx.smoke_test_id
        return headers

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None,
             ctx: CallContext | None = None) -> Any:
        if tool not in READ_TOOLS:
            raise ValueError(f"read client refused non-read tool: {tool}")
        context = ctx or CallContext()
        request_id = f"client-{time.time_ns()}"
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "method": "tools/call", "params": {
                                  "name": tool, "arguments": dict(arguments or {})}}).encode()
        request = Request(self.endpoint.url, data=payload, headers=self._headers(context))
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            raise BridgeReadTimeout(endpoint=self.endpoint.url, request_id=request_id,
                                    operation=tool,
                                    symbol=(arguments or {}).get("symbol"),
                                    elapsed_ms=(time.perf_counter() - started) * 1000) from exc
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeTransportError(str(exc)) from exc
        try:
            result = outer["result"]
            text = result["content"][0]["text"]
            if result.get("isError"):
                raise classify_tool_error(text)
            return json.loads(text)
        except BridgeTransportError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeTransportError("malformed bridge response") from exc

    def health(self) -> dict[str, Any]:
        try:
            with urlopen(self.endpoint.url.rsplit("/mcp", 1)[0] + "/health", timeout=10) as response:
                return json.loads(response.read())
        except Exception as exc:
            raise BridgeTransportError(str(exc)) from exc

    def list_tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list", {})["tools"]

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"jsonrpc": "2.0", "id": f"client-{time.time_ns()}",
                              "method": method, "params": params}).encode()
        try:
            with urlopen(Request(self.endpoint.url, data=payload,
                                 headers={"Content-Type": "application/json"}), timeout=self.timeout_s) as response:
                return json.loads(response.read())["result"]
        except Exception as exc:
            raise BridgeTransportError(str(exc)) from exc

    def terminal_info(self, ctx=None): return self.call("mt5_terminal_info", {}, ctx)
    def account_info(self, ctx=None): return self.call("mt5_account_info", {}, ctx)
    def symbols(self, ctx=None): return self.call("mt5_symbols", {}, ctx)
    def symbol_info(self, symbol, ctx=None): return self.call("mt5_symbol_info", {"symbol": symbol}, ctx)
    def quote(self, symbol, ctx=None): return self.call("mt5_quote", {"symbol": symbol}, ctx)
    def rates(self, symbol, timeframe, limit=20, ctx=None):
        return self.call("mt5_rates", {"symbol": symbol, "timeframe": timeframe, "limit": limit}, ctx)
    def rates_range(self, symbol, timeframe, start_timestamp, end_timestamp, page_size=500, completed_only=True, ctx=None):
        return self.call("mt5_rates_range", {"symbol": symbol, "timeframe": timeframe,
            "start_timestamp": start_timestamp, "end_timestamp": end_timestamp,
            "page_size": page_size, "completed_only": completed_only}, ctx)
    def symbol_snapshot(self, symbol, limit=320, ctx=None):
        return self.call("mt5_symbol_snapshot", {"symbol": symbol,
            "timeframes": ["M5", "M15", "H1", "H4"], "limit": limit}, ctx)
    def positions(self, ctx=None): return self.call("mt5_positions", {}, ctx)
    def orders(self, ctx=None): return self.call("mt5_orders", {}, ctx)
    def history(self, limit=100, ctx=None): return self.call("mt5_history", {"limit": limit}, ctx)
    def order_check(self, arguments, ctx=None): return self.call("mt5_order_check", arguments, ctx)


class Mt5ExecutionClient(Mt5ReadClient):
    """Transport-only write client; authorization is injected by the platform."""

    def call_write(self, tool: str, arguments: Mapping[str, Any], ctx: CallContext) -> Any:
        if tool not in WRITE_TOOLS:
            raise ValueError(f"execution client refused non-write tool: {tool}")
        payload = json.dumps({"jsonrpc": "2.0", "id": f"client-{time.time_ns()}",
                              "method": "tools/call", "params": {"name": tool,
                              "arguments": dict(arguments)}}).encode()
        try:
            with urlopen(Request(self.endpoint.url, data=payload, headers=self._headers(ctx)), timeout=10) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            raise BridgeAckUncertain(f"{tool.upper()}_ACK_UNCERTAIN_RECONCILE_REQUIRED") from exc
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeTransportError(str(exc)) from exc
        result = outer.get("result", {})
        text = result.get("content", [{}])[0].get("text", "")
        if result.get("isError"):
            raise classify_tool_error(text)
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise BridgeTransportError("malformed bridge write response") from exc

    def send_canonical_market_order(self, request, *, idempotency_key, ctx):
        args = dict(request)
        args["idempotency_key"] = idempotency_key
        return self.call_write("mt5_canonical_order_send", args, ctx)

    def close_position(self, ticket: int, ctx: CallContext):
        return self.call_write("mt5_close_position", {"ticket": int(ticket), "confirm": True}, ctx)

    def trailing_stop(self, ticket: int, distance_price: float, ctx: CallContext):
        return self.call_write("mt5_trailing_stop", {"ticket": int(ticket),
            "distance_price": float(distance_price), "confirm": True}, ctx)
