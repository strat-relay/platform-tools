from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from .store import TIMEFRAMES, MarketDataStore


class MarketDataUnavailable(RuntimeError):
    pass


def read_symbol_cached(store: MarketDataStore, symbol: str, limit: int = 320,
                       include_provenance: bool = False, *, now: float | None = None,
                       max_age: float | None = None) -> tuple[Any, ...]:
    now = time.time() if now is None else now
    max_age = float(os.getenv("MARKET_DATA_MAX_AGE_SECONDS", "330")) if max_age is None else max_age
    snapshot = store.snapshot(symbol)
    if snapshot is None:
        raise MarketDataUnavailable(f"market data cache has no snapshot for {symbol}")
    age = now - float(snapshot["fetched_at"])
    if age > max_age:
        raise MarketDataUnavailable(f"market data cache for {symbol} is stale ({age:.0f}s > {max_age:.0f}s)")
    need = max(limit - 1, 0)
    bars = {}
    for timeframe in TIMEFRAMES:
        rows = store.bars(symbol, timeframe)
        if len(rows) < need:
            raise MarketDataUnavailable(f"market data cache for {symbol} {timeframe} has {len(rows)} bars, needs {need}")
        bars[timeframe] = rows[-need:] if need else []
    contract, quote = snapshot["symbol_info"], snapshot["quote"]
    if not include_provenance:
        return contract, quote, bars
    quote_time = quote.get("time") if isinstance(quote, dict) else None
    producer = {
        "source_read_health": True,
        "source_market_data_timestamp": (datetime.fromtimestamp(int(quote_time), tz=timezone.utc).isoformat()
                                          if quote_time is not None else None),
        "provenance_source": "MARKET_DATA_CACHE",
    }
    return contract, quote, bars, producer


_STORE: MarketDataStore | None = None


def default_store() -> MarketDataStore:
    global _STORE
    if _STORE is None:
        import redis
        url = os.getenv("MARKET_DATA_REDIS_URL")
        if not url:
            raise MarketDataUnavailable("MARKET_DATA_REDIS_URL is required for MARKET_DATA_SOURCE=REDIS")
        _STORE = MarketDataStore(redis.Redis.from_url(url, socket_timeout=2.0))
    return _STORE
