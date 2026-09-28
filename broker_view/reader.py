"""Redis-first read-only broker reader.

Same `call(tool, arguments)` contract as platform_api.control.ReadOnlyBridgeReader, plus
`read(tool, arguments) -> (data, observed_at, source)`. A Redis entry younger than
`max_age_seconds` is served as-is; a missing or stale entry, or an unreachable Redis, falls back to
the wrapped bridge reader, so the answer is never older than the bridge path would allow.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

from .store import BrokerViewStore

log = logging.getLogger("broker_view")

SOURCE_REDIS = "redis_broker_view"
SOURCE_BRIDGE = "mt5_bridge_read_only"
CLOCK_SKEW_SECONDS = 5.0


class RedisFirstBridgeReader:
    def __init__(self, store: BrokerViewStore, fallback: Any, *, allowed_tools: Iterable[str],
                 max_age_seconds: float = 45.0, clock: Callable[[], float] = time.time):
        self.store = store
        self.fallback = fallback
        self.allowed_tools = frozenset(allowed_tools)
        self.max_age_seconds = max_age_seconds
        self.clock = clock

    @property
    def timeout(self) -> float:
        return float(getattr(self.fallback, "timeout", 5.0))

    def read(self, tool: str, arguments: dict[str, Any] | None = None) -> tuple[Any, float, str]:
        if tool not in self.allowed_tools:
            raise ValueError("Control API permits only read-only broker tools")
        try:
            entry = self.store.get(tool, arguments)
        except Exception as exc:  # noqa: BLE001 - Redis down: the bridge path still answers
            log.warning("broker view unavailable, reading the bridge: %s", exc)
            entry = None
        if entry is not None and -CLOCK_SKEW_SECONDS <= self.clock() - entry.observed_at <= self.max_age_seconds:
            return entry.data, entry.observed_at, SOURCE_REDIS
        data = self.fallback.call(tool, arguments)
        return data, self.clock(), SOURCE_BRIDGE

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        return self.read(tool, arguments)[0]
