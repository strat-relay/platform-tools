"""Read-only live snapshot adapter for the Liquidity evaluator.

The adapter is deliberately limited to the research/read bridge on 22347.  It has
no account, order, position, or execution method and resolves provider symbols from
the canonical mapping layer supplied by the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trade_management.runtime.market_data_live import ReadOnlyBridgeClient, build_bridge_client, resolve_broker_symbol


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        nested = value.get("rates") or value.get("bars") or value.get("data")
        return _rows(nested)
    return []


def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


class ReadOnlyLiquidityMarketData:
    def __init__(self, client: ReadOnlyBridgeClient):
        self.client = client

    def snapshot(self, canonical_instrument: str, provider_symbol: str | None = None) -> dict[str, Any]:
        symbol = provider_symbol or resolve_broker_symbol(canonical_instrument)
        quote = self.client.quote(symbol)
        contract = self.client.symbol_info(symbol)
        m5 = _rows(self.client.rates(symbol, "M5", limit=160))
        m15 = _rows(self.client.rates(symbol, "M15", limit=64))
        if not quote or not contract or not m5 or not m15:
            return {"source_kind": "LIVE_MARKET", "M5": m5, "M15": m15,
                    "quote": quote or {}, "contract": contract or {},
                    "provider_symbol": symbol}
        source_timestamp = quote.get("timestamp") or quote.get("time") or m5[-1].get("time")
        return {
            "source_kind": "LIVE_MARKET",
            "canonical_instrument": canonical_instrument,
            "provider_symbol": symbol,
            "M5": m5,
            "M15": m15,
            "quote": quote,
            "contract": contract,
            "source_market_data_timestamp": _timestamp(source_timestamp),
        }


def build_liquidity_market_data(mcp_url: str) -> ReadOnlyLiquidityMarketData:
    return ReadOnlyLiquidityMarketData(build_bridge_client(mcp_url))
