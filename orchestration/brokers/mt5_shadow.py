from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import secrets
import time
from typing import Any
from urllib.request import Request, urlopen

READ_ONLY_BRIDGE_TOOLS = frozenset({"mt5_account_info", "mt5_symbol_info", "mt5_quote", "mt5_rates",
                                   "mt5_positions", "mt5_orders", "mt5_history", "mt5_order_check"})


class BridgeQueueBacklog(RuntimeError):
    pass


class BridgeReadPathUnhealthy(BridgeQueueBacklog):
    """The read transport is not making progress; callers must back off."""


class BridgeReadTimeout(TimeoutError):
    """A read timed out, retaining the correlation data needed for diagnosis."""

    def __init__(self, *, endpoint: str, request_id: str, operation: str,
                 symbol: str | None, elapsed_ms: float):
        self.endpoint = endpoint
        self.request_id = request_id
        self.operation = operation
        self.symbol = symbol
        self.elapsed_ms = elapsed_ms
        detail = f"endpoint={endpoint} request_id={request_id} elapsed_ms={elapsed_ms:.1f}"
        if symbol:
            detail += f" symbol={symbol}"
        super().__init__(f"{operation.upper()}_TIMEOUT:{detail}")


class MT5ShadowProvider:
    def __init__(self, mcp_url: str, snapshot_ttl_seconds: int = 15,
                 caller: str = "UNKNOWN_LEGACY", health_interval_seconds: int = 20,
                 max_active_requests: int | None = None, read_timeout_seconds: int = 35,
                 max_age_ms: int | None = None):
        self.mcp_url = mcp_url
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self._snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.caller = caller
        self.health_interval_seconds = health_interval_seconds
        self.max_active_requests = max_active_requests if max_active_requests is not None else int(os.environ.get("MT5_BRIDGE_MAX_ACTIVE_READS", "4"))
        self.read_timeout_seconds = read_timeout_seconds
        self.max_age_ms = max_age_ms
        self.priority_class = os.environ.get("MT5_BRIDGE_PRIORITY_CLASS")
        if self.priority_class is None and any(caller.startswith(prefix) for prefix in (
                "ACCOUNT_VERIFICATION", "EXECUTION_CONSUMER", "RECONCILIATION",
                "EXECUTION_QUOTE", "ACCOUNT_SNAPSHOT_REFRESH")):
            self.priority_class = "EXECUTION_CRITICAL"
        self._next_allowed_read = 0.0
        self._backoff_seconds = 1.0
        self._account_context: str | None = None
        self.account_context_changed = False

    def _healthy_for_read(self, health: dict[str, Any]) -> bool:
        lifecycle = health.get("lifecycle", {}) or {}
        active = lifecycle.get("active_waiters", lifecycle.get("queued_active", 0) + lifecycle.get("dispatched_active", 0))
        age = health.get("seconds_since_last_response")
        if age is None and lifecycle.get("last_response_timestamp"):
            age = max(0.0, time.time() - float(lifecycle["last_response_timestamp"]))
        read_ok = lifecycle.get("read_path_healthy", health.get("read_path_healthy", True))
        poll_ok = lifecycle.get("poll_path_healthy")
        response_ok = age is None or age <= self.health_interval_seconds
        # An idle bridge may have no recent broker response while its EA is
        # actively polling. In that case allow one guarded read probe; the
        # request itself remains subject to the normal timeout/backoff path.
        transport_ok = response_ok or poll_ok is True
        return bool(read_ok) and int(active or 0) <= self.max_active_requests and transport_ok

    def _record_success(self) -> None:
        self._next_allowed_read = 0.0
        self._backoff_seconds = 1.0

    def _record_failure(self) -> None:
        self._next_allowed_read = time.time() + self._backoff_seconds
        self._backoff_seconds = min(120.0, self._backoff_seconds * 2.0)

    def _read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ONLY_BRIDGE_TOOLS:
            raise RuntimeError(f"shadow provider refused non-read-only tool: {name}")
        if time.time() < self._next_allowed_read:
            raise BridgeReadPathUnhealthy("BRIDGE_READ_BACKOFF")
        health = self.health()
        if "lifecycle" not in health and int(health.get("pending", 0) or 0) > 0:
            self._record_failure()
            raise BridgeQueueBacklog(f"BRIDGE_QUEUE_BACKLOG pending={health['pending']}")
        if not self._healthy_for_read(health):
            self._record_failure()
            lifecycle = health.get("lifecycle", {}) or {}
            raise BridgeReadPathUnhealthy(
                "BRIDGE_READ_PATH_UNHEALTHY "
                f"active={lifecycle.get('active_waiters', 'N/A')} "
                f"transport_queue={lifecycle.get('transport_queue_depth', health.get('pending', 'N/A'))}"
            )
        request_id = secrets.token_hex(16)
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}}).encode()
        started = time.perf_counter()
        request = Request(self.mcp_url, data=payload, headers={
            "Content-Type": "application/json", "X-Bridge-Origin": self.caller,
            "X-Bridge-Origin-Pid": str(os.getpid()),
            "X-Bridge-Cache-Control": "no-cache" if self.snapshot_ttl_seconds == 0 else "",
            "X-Bridge-Max-Age-Ms": str(self.max_age_ms) if self.max_age_ms is not None else "",
            "X-Bridge-Priority-Class": self.priority_class or "",
        })
        try:
            with urlopen(request, timeout=self.read_timeout_seconds) as response:
                outer = json.loads(response.read())
        except TimeoutError as exc:
            self._record_failure()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            raise BridgeReadTimeout(endpoint=self.mcp_url, request_id=request_id,
                                    operation=name, symbol=arguments.get("symbol"),
                                    elapsed_ms=elapsed_ms) from exc
        except Exception:
            self._record_failure()
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.last_request = {"request_id": request_id, "operation": name,
                             "symbol": arguments.get("symbol"), "endpoint": self.mcp_url,
                             "response_elapsed_ms": elapsed_ms}
        result = outer["result"]
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        value = json.loads(result["content"][0]["text"])
        self._record_success()
        return value

    def health(self) -> dict[str, Any]:
        # The execution bridge health payload includes bounded lifecycle
        # counters.  Under Wine it can take slightly over two seconds to
        # serialize even when the bridge and EA are healthy; keep this read
        # probe read-only but avoid misclassifying that response as down.
        with urlopen(self.mcp_url.rsplit("/mcp", 1)[0] + "/health", timeout=10) as response:
            return json.loads(response.read())

    def account_snapshot(self, account_id: str) -> dict[str, Any]:
        cached = self._snapshot_cache.get(account_id)
        if cached and time.time() - cached[0] <= self.snapshot_ttl_seconds:
            age = time.time() - cached[0]
            return dict(cached[1], snapshot_cache_hit=True, snapshot_age_seconds=age,
                        age_ms=age * 1000.0, freshness_state="FRESH" if age <= self.snapshot_ttl_seconds else "AGING")
        data = self._read("mt5_account_info", {})
        raw_context = data.get("login") or data.get("account") or data.get("account_id")
        server = data.get("server") or data.get("company")
        context = f"{raw_context}@{server}" if raw_context is not None and server else str(raw_context or server or "UNKNOWN")
        self.account_context_changed = self._account_context is not None and context != self._account_context
        if self.account_context_changed:
            self._snapshot_cache.clear()
        self._account_context = context
        retrieved = datetime.now(timezone.utc).isoformat()
        snapshot = {"account_id": account_id, "account_context_id": context,
                "account_context_changed": self.account_context_changed,
                "timestamp": data.get("timestamp") or retrieved,
                "snapshot_timestamp": data.get("timestamp") or retrieved,
                "retrieval_timestamp": retrieved,
                "balance": data.get("balance"), "equity": data.get("equity"), "margin": data.get("margin"),
                "free_margin": data.get("free_margin"), "margin_level": data.get("margin_level"),
                "currency": data.get("currency"), "leverage": data.get("leverage"), "raw": data}
        self._snapshot_cache[account_id] = (time.time(), snapshot)
        return dict(snapshot, snapshot_cache_hit=False, snapshot_age_seconds=0.0, age_ms=0.0, freshness_state="FRESH")

    def symbol_metadata(self, symbol: str) -> dict[str, Any]:
        data = self._read("mt5_symbol_info", {"symbol": symbol})
        return {"broker_symbol": symbol, "canonical_symbol": symbol.rstrip("m"),
                "contract_size": data.get("contract_size"), "tick_size": data.get("tick_size", data.get("point")),
                "point": data.get("point", data.get("tick_size")),
                "tick_value": data.get("tick_value"), "volume_min": data.get("min_lot", data.get("volume_min")),
                "volume_max": data.get("max_lot", data.get("volume_max")), "volume_step": data.get("lot_step", data.get("volume_step")),
                "stops_level": data.get("stops_level"), "freeze_level": data.get("freeze_level"),
                "trade_enabled": data.get("trade_enabled"), "filling_mode": data.get("filling_mode"),
                "order_mode": data.get("order_mode"), "expiration_mode": data.get("expiration_mode"),
                "currency": data.get("currency"), "raw": data}

    def order_check(self, *, symbol: str, side: str, volume: float,
                    stop_loss: float, take_profit: float,
                    canonical_request: dict[str, Any] | None = None,
                    canonical_request_text: str | None = None) -> dict[str, Any]:
        """Read-only MT5 OrderCheck through the dedicated bridge."""
        args = {"symbol": symbol, "side": side, "volume": volume,
                "stop_loss": stop_loss, "take_profit": take_profit}
        if canonical_request is not None:
            args["canonical_request"] = dict(canonical_request)
            if canonical_request_text is not None:
                args["canonical_request_text"] = canonical_request_text
        return self._read("mt5_order_check", args)

    def quote(self, symbol: str) -> dict[str, Any]:
        """Read a fresh executable-market quote through the configured bridge.

        This deliberately uses the same guarded read path as account and
        symbol reads.  It does not consult the research bridge or a shared
        quote cache, which is required for smoke-test preflight freshness.
        """
        data = self._read("mt5_quote", {"symbol": symbol})
        return {
            "broker_symbol": symbol,
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "last": data.get("last"),
            "spread_points": data.get("spread_points"),
            "timestamp": data.get("time") or data.get("timestamp"),
            "raw": data,
        }

    def open_positions(self, account_id: str) -> list[dict[str, Any]]:
        value = self._read("mt5_positions", {})
        return value if isinstance(value, list) else []

    def pending_orders(self, account_id: str) -> list[dict[str, Any]]:
        value = self._read("mt5_orders", {})
        return value if isinstance(value, list) else []

    def history(self, limit: int = 100) -> dict[str, Any]:
        value = self._read("mt5_history", {"limit": int(limit)})
        return value if isinstance(value, dict) else {"deals": []}
