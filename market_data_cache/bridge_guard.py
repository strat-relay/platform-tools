"""Global protection for the read-only MT5 bridge.

This module owns transport pressure, not market-data or strategy semantics.  A bridge-global
failure is recorded once for the collector and does not become a separate failure for every
symbol in the same pass.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

log = logging.getLogger("market_data_cache")

BRIDGE_GLOBAL = "BRIDGE_GLOBAL"
EA_UNAVAILABLE = "EA_UNAVAILABLE"
BRIDGE_BACKPRESSURE = "BRIDGE_BACKPRESSURE"
BRIDGE_QUEUE_SATURATED = "BRIDGE_QUEUE_SATURATED"
BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
BRIDGE_TRANSPORT_ERROR = "BRIDGE_TRANSPORT_ERROR"


class BridgeCircuitOpen(RuntimeError):
    classification = "BRIDGE_CIRCUIT_OPEN"


def classify_bridge_failure(exc: Exception) -> str | None:
    detail = str(exc).lower()
    if "bridge backpressure: queue depth" in detail:
        return BRIDGE_BACKPRESSURE
    if "queue depth" in detail and (">=" in detail or "saturat" in detail):
        return BRIDGE_QUEUE_SATURATED
    if "ea did not respond" in detail or "allow webrequest" in detail:
        return EA_UNAVAILABLE
    if isinstance(exc, TimeoutError) or "timeout" in detail or "timed out" in detail:
        return BRIDGE_TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)) or "transport" in detail or "unreachable" in detail:
        return BRIDGE_TRANSPORT_ERROR
    return None


def queue_pressure(detail: str) -> tuple[int | None, int | None]:
    match = re.search(r"queue depth\s+(\d+)\s*>=\s*(\d+)", detail.lower())
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


class BridgeCircuitBreaker:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, *, failure_threshold: int = 3, initial_backoff: float = 5.0,
                 max_backoff: float = 60.0, half_open_probes: int = 1,
                 clock: Callable[[], float] = time.time,
                 random_source: random.Random | None = None):
        self.failure_threshold = max(1, failure_threshold)
        self.initial_backoff = max(0.1, initial_backoff)
        self.max_backoff = max(self.initial_backoff, max_backoff)
        self.half_open_probes = max(1, half_open_probes)
        self.clock = clock
        self.random = random_source or random.Random()
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.backpressure_events = 0
        self.circuit_open_count = 0
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self.next_probe_at: float | None = None
        self._half_open_probes_used = 0
        self.queue_depth: int | None = None
        self.queue_limit: int | None = None
        self._lock = threading.RLock()

    def _event(self, event: str, **fields: Any) -> None:
        log.info("%s", json.dumps({"event": event, **fields}, sort_keys=True, default=str))

    def allow(self, operation: str, *, probe_operation: str = "mt5_terminal_info") -> None:
        with self._lock:
            now = self.clock()
            if self.state == self.OPEN:
                if now < float(self.next_probe_at or now):
                    raise BridgeCircuitOpen("bridge circuit is OPEN")
                self.state = self.HALF_OPEN
                self._half_open_probes_used = 0
                self._event("bridge_circuit_half_open")
            if self.state == self.HALF_OPEN:
                if operation != probe_operation or self._half_open_probes_used >= self.half_open_probes:
                    raise BridgeCircuitOpen("bridge circuit is HALF_OPEN; probe required")
                self._half_open_probes_used += 1

    def probe_due(self) -> bool:
        with self._lock:
            return self.state == self.HALF_OPEN or (self.state == self.OPEN and
                                                     self.clock() >= float(self.next_probe_at or 0))

    def record_success(self, operation: str) -> None:
        with self._lock:
            now = self.clock()
            was_half_open = self.state == self.HALF_OPEN
            downtime = now - self.last_success_at if self.last_success_at is not None else None
            self.last_success_at = now
            self.last_error = None
            self.consecutive_failures = 0
            self.queue_depth = self.queue_limit = None
            self.state = self.CLOSED
            self.next_probe_at = None
            if was_half_open:
                self._event("bridge_circuit_closed", downtime_seconds=downtime, operation=operation)

    def record_failure(self, exc: Exception, operation: str) -> str | None:
        classification = classify_bridge_failure(exc)
        if classification is None:
            return None
        with self._lock:
            self.consecutive_failures += 1
            self.last_error = f"{classification}: {exc}"[:500]
            depth, limit = queue_pressure(str(exc))
            if depth is not None:
                self.queue_depth, self.queue_limit = depth, limit
                self.backpressure_events += 1
                self._event("bridge_backpressure_detected", reported_queue_depth=depth, queue_limit=limit)
            immediate_open = classification in {BRIDGE_BACKPRESSURE, BRIDGE_QUEUE_SATURATED}
            if self.state == self.HALF_OPEN or immediate_open or self.consecutive_failures >= self.failure_threshold:
                exponent = max(0, self.consecutive_failures - self.failure_threshold)
                base = min(self.max_backoff, self.initial_backoff * (2 ** exponent))
                backoff = min(self.max_backoff, base * (0.8 + self.random.random() * 0.4))
                was_open = self.state == self.OPEN
                self.state = self.OPEN
                self.next_probe_at = self.clock() + backoff
                if not was_open:
                    self.circuit_open_count += 1
                    self._event("bridge_circuit_opened", reason=classification,
                                failure_count=self.consecutive_failures, backoff_seconds=backoff)
            return classification

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"bridge_circuit_state": self.state,
                    "bridge_health_state": "BRIDGE_UNAVAILABLE" if self.state != self.CLOSED else "AVAILABLE",
                    "bridge_queue_pressure": self.queue_depth,
                    "bridge_queue_limit": self.queue_limit,
                    "bridge_consecutive_failures": self.consecutive_failures,
                    "bridge_backpressure_events": self.backpressure_events,
                    "bridge_circuit_open_count": self.circuit_open_count,
                    "bridge_last_success_at": self.last_success_at,
                    "bridge_last_error": self.last_error,
                    "bridge_next_probe_at": self.next_probe_at}


class BridgeRequestGate:
    """Bound and coalesce collector calls while preserving the synchronous client API."""

    def __init__(self, read_tool: Callable[[str, dict[str, Any]], Any], breaker: BridgeCircuitBreaker,
                 *, max_inflight: int = 4, clock: Callable[[], float] = time.time):
        self.read_tool = read_tool
        self.breaker = breaker
        self.semaphore = threading.BoundedSemaphore(max(1, max_inflight))
        self.max_inflight = max(1, max_inflight)
        self.inflight: dict[str, Future[Any]] = {}
        self.lock = threading.RLock()
        self.clock = clock
        self.requests_coalesced_total = 0
        self.inflight_count = 0

    @staticmethod
    def key(operation: str, arguments: dict[str, Any]) -> str:
        return json.dumps([operation, arguments], sort_keys=True, separators=(",", ":"), default=str)

    def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        key = self.key(operation, arguments)
        with self.lock:
            existing = self.inflight.get(key)
            if existing is not None:
                self.requests_coalesced_total += 1
                log.info("%s", json.dumps({"event": "bridge_request_coalesced", "operation": operation,
                                            "provider_symbol": arguments.get("symbol")}, sort_keys=True))
                owner = False
                future = existing
            else:
                self.breaker.allow(operation)
                future = Future()
                self.inflight[key] = future
                owner = True
        if not owner:
            return future.result()
        self.semaphore.acquire()
        with self.lock:
            self.inflight_count += 1
        try:
            result = self.read_tool(operation, arguments)
            self.breaker.record_success(operation)
            future.set_result(result)
            return result
        except Exception as exc:
            self.breaker.record_failure(exc, operation)
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.inflight.pop(key, None)
                self.inflight_count -= 1
            self.semaphore.release()
