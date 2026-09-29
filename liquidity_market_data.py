"""Read-only live snapshot adapter for the Liquidity evaluator.

The adapter is deliberately limited to the research/read bridge on 22347.  It has
no account, order, position, or execution method and resolves provider symbols from
the canonical mapping layer supplied by the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from trade_management.runtime.market_data_live import ReadOnlyBridgeClient, build_bridge_client, resolve_broker_symbol


@dataclass(frozen=True)
class LiveMarketSnapshot:
    """Validated snapshot emitted only by the read-only market-data boundary."""
    M5: tuple[dict[str, Any], ...]
    M15: tuple[dict[str, Any], ...]
    quote: dict[str, Any]
    contract: dict[str, Any]
    canonical_instrument: str
    provider_symbol: str
    source_market_data_timestamp: str
    source_kind: str = "LIVE_MARKET"
    validated_by: str = "mt5-read-22347"


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        nested = value.get("rates") or value.get("bars") or value.get("data")
        return _rows(nested)
    return []


def _completed_rows(value: Any) -> list[dict[str, Any]]:
    rows = _rows(value)
    # The bridge returns the currently-forming candle last, matching the existing
    # paper/forward readers. Only completed candles may reach the evaluator.
    return rows[:-1] if len(rows) > 1 else []


def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


class ReadOnlyLiquidityMarketData:
    def __init__(self, client: ReadOnlyBridgeClient):
        self.client = client

    def snapshot(self, canonical_instrument: str, provider_symbol: str | None = None) -> LiveMarketSnapshot:
        symbol = provider_symbol or resolve_broker_symbol(canonical_instrument)
        quote = self.client.quote(symbol)
        contract = self.client.symbol_info(symbol)
        m5 = _completed_rows(self.client.rates(symbol, "M5", limit=160))
        m15 = _completed_rows(self.client.rates(symbol, "M15", limit=64))
        if not quote or not contract or not m5 or not m15:
            return LiveMarketSnapshot(tuple(m5), tuple(m15), quote or {}, contract or {},
                                      canonical_instrument, symbol, "")
        source_timestamp = quote.get("timestamp") or quote.get("time") or m5[-1].get("time")
        return LiveMarketSnapshot(tuple(m5), tuple(m15), quote, contract,
                                  canonical_instrument, symbol, _timestamp(source_timestamp))


# Bar windows the evaluator receives (completed candles), identical to the bridge reads above:
# rates(limit=160) / rates(limit=64) minus the forming candle.
M5_COMPLETED, M15_COMPLETED = 159, 63


class CachedLiquidityMarketData:
    """MARKET_DATA_SOURCE=REDIS: the same LiveMarketSnapshot from the market-data cache
    (market_data_cache/, kept current by one collector), with no bridge call. The quote and
    contract come from the same real bridge snapshot as the bars. Missing, stale or shallow data
    raises, exactly like a failed bridge read."""

    def __init__(self, store: Any, *, max_age: float | None = None, clock: Any = None):
        import os
        import time
        self.store = store
        self.max_age = float(os.getenv("MARKET_DATA_MAX_AGE_SECONDS", "330")) if max_age is None else max_age
        self.clock = clock or time.time

    def snapshot(self, canonical_instrument: str, provider_symbol: str | None = None) -> LiveMarketSnapshot:
        from market_data_cache.reader import MarketDataUnavailable
        symbol = provider_symbol or resolve_broker_symbol(canonical_instrument)
        cached = self.store.snapshot(symbol)
        if cached is None:
            raise MarketDataUnavailable(f"market data cache has no snapshot for {symbol}")
        if self.clock() - float(cached["fetched_at"]) > self.max_age:
            raise MarketDataUnavailable(f"market data cache for {symbol} is stale")
        m5, m15 = self.store.bars(symbol, "M5"), self.store.bars(symbol, "M15")
        if len(m5) < M5_COMPLETED or len(m15) < M15_COMPLETED:
            raise MarketDataUnavailable(f"market data cache for {symbol} is not deep enough yet")
        quote, contract = cached["quote"], cached["symbol_info"]
        m5, m15 = m5[-M5_COMPLETED:], m15[-M15_COMPLETED:]
        source_timestamp = quote.get("timestamp") or quote.get("time") or m5[-1].get("time")
        return LiveMarketSnapshot(tuple(m5), tuple(m15), quote, contract, canonical_instrument, symbol,
                                  _timestamp(source_timestamp), validated_by="market-data-cache")


def build_liquidity_market_data(mcp_url: str) -> ReadOnlyLiquidityMarketData | CachedLiquidityMarketData:
    import os
    if os.getenv("MARKET_DATA_SOURCE", "BRIDGE").strip().upper() == "REDIS":
        from market_data_cache.reader import default_store
        return CachedLiquidityMarketData(default_store())
    return ReadOnlyLiquidityMarketData(build_bridge_client(mcp_url))
