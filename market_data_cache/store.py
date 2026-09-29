from __future__ import annotations

import json
from typing import Any

TIMEFRAMES = ("M5", "M15", "H1", "H4")


class MarketDataStore:
    def __init__(self, redis_client: Any):
        self.redis = redis_client

    def _get(self, key: str) -> Any:
        raw = self.redis.get(key)
        return None if raw is None else json.loads(raw)

    def bars(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        return self._get(f"md:bars:{symbol}:{timeframe}") or []

    def _set(self, key: str, value: Any) -> None:
        self.redis.set(key, json.dumps(value, sort_keys=True, default=str))

    def set_bars(self, symbol: str, timeframe: str, rows: list[dict[str, Any]]) -> None:
        self._set(f"md:bars:{symbol}:{timeframe}", rows[-400:])

    def snapshot(self, symbol: str) -> dict[str, Any] | None:
        return self._get(f"md:snapshot:{symbol}")

    def set_snapshot(self, symbol: str, value: dict[str, Any]) -> None:
        self._set(f"md:snapshot:{symbol}", value)

    def metadata(self, symbol: str) -> dict[str, Any] | None:
        return self._get(f"md:meta:{symbol}")

    def set_metadata(self, symbol: str, symbol_info: dict[str, Any], observed_at: float) -> None:
        self._set(f"md:meta:{symbol}", {"symbol_info": symbol_info, "observed_at": observed_at})

    def state(self, symbol: str) -> dict[str, Any]:
        return self._get(f"md:state:{symbol}") or {}

    def set_state(self, symbol: str, value: dict[str, Any]) -> None:
        self._set(f"md:state:{symbol}", value)

    def health(self) -> dict[str, Any]:
        return self._get("md:health") or {}

    def set_health(self, value: dict[str, Any]) -> None:
        self._set("md:health", value)
