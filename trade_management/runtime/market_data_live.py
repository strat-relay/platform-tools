"""The production `MarketDataProvider` (`trade_management/market_data.py`) adapter, over the
existing read-only 22347 research listener - never port 22348, never a broker position/order.

Built on `contracts.mt5_bridge.Mt5ReadClient`, the same sanctioned read-only client already used
in production by `paper_runner.py` and (per `docs/runtime_boundaries` P1) the only bridge client
the production execution path itself is permitted to use for reads. `Mt5ReadClient.call()`
hard-refuses any tool outside `READ_TOOLS` at the transport layer (`ValueError`) - this module
adds no write capability whatsoever on top of that; it only ever calls `.quote()`/`.rates()`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from trade_management.market_data import BarWindow, MarketQuote

PROVIDER_ID = "mt5-bridge-research-22347"


class ReadOnlyBridgeClient(Protocol):
    """Structural subset of `contracts.mt5_bridge.Mt5ReadClient` this adapter needs - kept
    narrow and duck-typed so tests can substitute a fake transport without importing the bridge
    package at all."""

    def quote(self, symbol: str) -> dict[str, Any]: ...
    def rates(self, symbol: str, timeframe: str, limit: int = 20) -> Any: ...


def build_bridge_client(mcp_url: str) -> ReadOnlyBridgeClient:
    """Constructs the real `Mt5ReadClient`. Isolated in its own function (rather than imported
    at module scope by callers) so tests exercise `LiveMarketDataProvider` against a fake
    transport without ever importing `contracts.mt5_bridge` themselves, and so this is the one
    place a future non-MT5 provider swap would change."""
    if ":22348" in mcp_url:
        raise ValueError("live market data adapter must never target the execution port (22348)")
    from contracts.mt5_bridge import BridgeEndpoint, Mt5ReadClient
    return Mt5ReadClient(BridgeEndpoint(mcp_url, profile="research"))


class LiveMarketDataProvider:
    """Implements `trade_management.market_data.MarketDataProvider`. Read-only, no account, no
    ticket, no broker position/order - the underlying client's tool allowlist makes this
    structural, not just a convention this class happens to follow."""

    def __init__(self, client: ReadOnlyBridgeClient, *, provider_id: str = PROVIDER_ID) -> None:
        self.client = client
        self.provider_id = provider_id

    def quote(self, instrument: str) -> MarketQuote:
        raw = self.client.quote(instrument)
        bid, ask = float(raw["bid"]), float(raw["ask"])
        source_timestamp = raw.get("timestamp") or raw.get("time")
        if not source_timestamp:
            age_ms = float(raw.get("quote_age_ms") or 0)
            observed_at = datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)
            source_timestamp = observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return MarketQuote(instrument=instrument, bid=bid, ask=ask, source_timestamp=str(source_timestamp),
                          provider_id=self.provider_id, feed_id=raw.get("feed_id"),
                          data_status="FORWARD")

    def bars(self, instrument: str, *, timeframe: str = "M5") -> BarWindow | None:
        raw = self.client.rates(instrument, timeframe, limit=1)
        rows = raw if isinstance(raw, list) else raw.get("rates") if isinstance(raw, dict) else None
        if not rows:
            return None
        last = rows[-1]
        completed_through = str(last.get("time") or last.get("timestamp") or "")
        digest = "sha256:" + hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        return BarWindow(timeframe=timeframe, completed_through=completed_through, count=len(rows), digest=digest)
