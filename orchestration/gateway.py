"""Read-only broker data gateway with coalescing and freshness metadata."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


class BrokerDataGateway:
    """Single read-only boundary for new components.

    ``reader`` is injected so tests and future broker adapters can be used
    without exposing order operations.  No method in this class accepts or
    forwards order requests.
    """

    READS = frozenset({"account_info", "symbol_info", "quote", "rates"})

    def __init__(self, reader: Callable[[str, dict[str, Any]], dict[str, Any]], ttl_seconds: dict[str, float] | None = None):
        self._reader = reader
        self._ttl = {"account_info": 3.0, "symbol_info": 10.0, "quote": 0.5, "rates": 1.0}
        if ttl_seconds:
            self._ttl.update(ttl_seconds)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, dict[str, Any], str]] = {}
        self._inflight: dict[str, tuple[threading.Event, dict[str, Any]]] = {}

    @staticmethod
    def _key(method: str, args: dict[str, Any]) -> str:
        return json.dumps([method, args], sort_keys=True, separators=(",", ":"))

    def read(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        if method not in self.READS:
            raise ValueError(f"BrokerDataGateway allows read methods only: {method}")
        key = self._key(method, args)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self._ttl[method]:
                return dict(cached[1], cache_hit=True, source_request_id=cached[2], age_seconds=now - cached[0])
            existing = self._inflight.get(key)
            if existing:
                event, box = existing
            else:
                event, box = threading.Event(), {}
                self._inflight[key] = (event, box)
                existing = None
        if existing:
            if not event.wait(self._ttl[method]):
                raise TimeoutError(f"coalesced broker read expired: {method}")
            if "error" in box:
                raise box["error"]
            return dict(box["value"], cache_hit=True, source_request_id=box["source_request_id"], age_seconds=0.0)
        try:
            value = dict(self._reader(method, args))
            source_request_id = str(value.pop("request_id", f"gateway-{time.time_ns()}"))
            retrieved = time.time()
            value.setdefault("source_timestamp", value.get("timestamp"))
            value["retrieved_at"] = datetime.now(timezone.utc).isoformat()
            value["age_seconds"] = 0.0
            with self._lock:
                self._cache[key] = (retrieved, value, source_request_id)
                box.update(value=value, source_request_id=source_request_id)
                self._inflight.pop(key, None)
                event.set()
            return dict(value, cache_hit=False, source_request_id=source_request_id)
        except Exception as exc:
            with self._lock:
                box["error"] = exc
                self._inflight.pop(key, None)
                event.set()
            raise

    def account_snapshot(self, account_id: str) -> dict[str, Any]:
        return self.read("account_info", {"account_id": account_id})

    def symbol_metadata(self, symbol: str) -> dict[str, Any]:
        return self.read("symbol_info", {"symbol": symbol})

    def quote(self, symbol: str) -> dict[str, Any]:
        return self.read("quote", {"symbol": symbol})

    def rates(self, symbol: str, timeframe: str = "M5", limit: int = 20) -> dict[str, Any]:
        return self.read("rates", {"symbol": symbol, "timeframe": timeframe, "limit": limit})

    def health(self) -> dict[str, Any]:
        return {"read_only": True, "cache_entries": len(self._cache), "inflight_reads": len(self._inflight)}
