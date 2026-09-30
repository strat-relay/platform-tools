"""Readers of the market-data cache. They never call the bridge: missing or stale data raises,
exactly as a failed bridge read would, and the caller's existing failure handling applies."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from .store import TIMEFRAMES, MarketDataStore


class MarketDataUnavailable(RuntimeError):
    pass


def _max_age(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def read_symbol_cached(store: MarketDataStore, symbol: str, limit: int = 320, include_provenance: bool = False,
                       *, now: float | None = None, max_age: float | None = None) -> tuple[Any, ...]:
    """Drop-in for the Context runner's read_symbol(): (contract, quote, bars[, producer]).

    Returns the last `limit - 1` completed bars per timeframe - what a fresh `mt5_symbol_snapshot`
    with `limit` rows yields after dropping the forming bar - plus the contract and quote of the
    same real snapshot. The data must be at most MARKET_DATA_MAX_AGE_SECONDS old (default 330 s:
    one M5 bar plus grace), so at most one closed bar can arrive late, never be skipped."""
    now = time.time() if now is None else now
    max_age = _max_age("MARKET_DATA_MAX_AGE_SECONDS", 330.0) if max_age is None else max_age
    from .canonical import CanonicalMarketDataReader
    canonical = CanonicalMarketDataReader(store, max_age=max_age, clock=lambda: now)
    snapshot = canonical.get_snapshot(symbol, symbol, {tf: max(limit - 1, 0) for tf in TIMEFRAMES})
    bars = {tf: list(snapshot.bars[tf].bars) for tf in TIMEFRAMES}
    contract, quote = snapshot.symbol_info, snapshot.quote
    if not include_provenance:
        return contract, quote, bars
    quote_time = quote.get("time") if isinstance(quote, dict) else None
    producer = {"source_read_health": True,
                "source_market_data_timestamp": (datetime.fromtimestamp(int(quote_time), tz=timezone.utc).isoformat()
                                                 if quote_time is not None else None),
                "provenance_source": "MARKET_DATA_CACHE",
                "canonical_source": snapshot.source,
                "cache_age_seconds": snapshot.cache_age_seconds,
                "continuity_status": snapshot.continuity_status,
                "gap_status": snapshot.gap_status,
                "recovery_status": snapshot.recovery_status}
    return contract, quote, bars, producer


def read_metadata(store: MarketDataStore, symbol: str, *, now: float | None = None,
                  max_age: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    max_age = _max_age("MARKET_METADATA_MAX_AGE_SECONDS", 900.0) if max_age is None else max_age
    meta = store.metadata(symbol)
    if meta is None or now - float(meta["observed_at"]) > max_age:
        raise MarketDataUnavailable(f"symbol metadata for {symbol} is missing or stale")
    return meta["symbol_info"]


class CachedQuoteClient:
    """For the Trade Manager's ReadOnlyBridgeClient: quote() from the cache (fails closed when
    missing/stale), rates() passed through to the bridge client unchanged."""

    def __init__(self, store: MarketDataStore, bridge_client: Any, *, max_age: float | None = None,
                 clock: Any = time.time):
        self.store, self.bridge_client, self.clock = store, bridge_client, clock
        self.max_age = _max_age("TM_QUOTE_MAX_AGE_SECONDS", 15.0) if max_age is None else max_age

    def quote(self, symbol: str) -> dict[str, Any]:
        cached = self.store.quote(symbol)
        if cached is None:
            raise MarketDataUnavailable(f"no cached quote for {symbol}")
        if self.clock() - float(cached["observed_at"]) > self.max_age:
            raise MarketDataUnavailable(f"cached quote for {symbol} is stale")
        return cached["quote"]

    def rates(self, symbol: str, timeframe: str, limit: int = 20) -> Any:
        return self.bridge_client.rates(symbol, timeframe, limit=limit)


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
