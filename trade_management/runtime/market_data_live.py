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
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from trade_management.market_data import BarWindow, MarketQuote

PROVIDER_ID = "mt5-bridge-research-22347"


class MarketDataError(RuntimeError):
    """Base class for a failed read-only market-data observation."""


class BrokerSymbolUnavailable(MarketDataError):
    """The resolved broker symbol is not available at the broker boundary."""


class QuoteUnavailable(MarketDataError):
    """The bridge answered, but no usable quote was available."""


class BridgeReadFailed(MarketDataError):
    """The read-only bridge could not complete the request."""


class MalformedBridgeQuote(MarketDataError):
    """The bridge response did not contain a valid bid/ask quote."""


class ReadOnlyBridgeClient(Protocol):
    """Structural subset of `contracts.mt5_bridge.Mt5ReadClient` this adapter needs - kept
    narrow and duck-typed so tests can substitute a fake transport without importing the bridge
    package at all."""

    def quote(self, symbol: str) -> dict[str, Any]: ...
    def rates(self, symbol: str, timeframe: str, limit: int = 20) -> Any: ...


def _broker_symbol_map() -> dict[str, str]:
    """Read the existing explicit platform mapping authority.

    The mapping is configuration, not a suffix convention.  An optional JSON override is
    provided for account-specific deployments and is deliberately validated as a complete
    canonical-to-broker mapping.
    """
    raw = os.environ.get("P4_BROKER_SYMBOL_MAP_JSON", "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MarketDataError("invalid P4_BROKER_SYMBOL_MAP_JSON") from exc
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v
                                                   for k, v in value.items()):
            raise MarketDataError("P4_BROKER_SYMBOL_MAP_JSON must contain string pairs")
        return value
    config_path = Path(__file__).resolve().parents[2] / "orchestration" / "config" / "platform.json"
    try:
        value = json.loads(config_path.read_text(encoding="utf-8")).get("symbol_mappings", {})
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketDataError(f"platform symbol mapping unavailable: {config_path}") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v
                                               for k, v in value.items()):
        raise MarketDataError("platform symbol_mappings must contain string pairs")
    return value


def resolve_broker_symbol(canonical_instrument: str) -> str:
    try:
        return _broker_symbol_map()[canonical_instrument]
    except KeyError as exc:
        raise BrokerSymbolUnavailable(
            f"no broker symbol mapping for canonical instrument {canonical_instrument!r}") from exc


def _normalize_source_timestamp(value: Any) -> str:
    """Convert bridge epoch seconds to the ISO timestamp contract used by Postgres."""
    if isinstance(value, bool):
        raise MalformedBridgeQuote("MALFORMED_BRIDGE_QUOTE:timestamp")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if not text:
            raise MalformedBridgeQuote("MALFORMED_BRIDGE_QUOTE:timestamp")
        return text
    try:
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as exc:
        raise MalformedBridgeQuote("MALFORMED_BRIDGE_QUOTE:timestamp") from exc


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
        broker_symbol = resolve_broker_symbol(instrument)
        try:
            raw = self.client.quote(broker_symbol)
        except Exception as exc:
            from contracts.mt5_bridge.errors import BridgeReadTimeout, BridgeToolError, BridgeTransportError
            if isinstance(exc, BridgeReadTimeout):
                raise BridgeReadFailed(f"BRIDGE_TIMEOUT:{exc}") from exc
            if isinstance(exc, BridgeToolError) and re.search(r"symbol|instrument|not found|unknown", str(exc), re.I):
                raise BrokerSymbolUnavailable(f"BROKER_SYMBOL_UNAVAILABLE:{broker_symbol}:{exc}") from exc
            if isinstance(exc, (BridgeToolError, BridgeTransportError)):
                raise QuoteUnavailable(f"QUOTE_UNAVAILABLE:{broker_symbol}:{exc}") from exc
            raise BridgeReadFailed(f"BRIDGE_READ_FAILED:{broker_symbol}:{exc}") from exc
        if not isinstance(raw, dict) or raw.get("error"):
            raise QuoteUnavailable(f"QUOTE_UNAVAILABLE:{broker_symbol}")
        try:
            bid, ask = float(raw["bid"]), float(raw["ask"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedBridgeQuote(f"MALFORMED_BRIDGE_QUOTE:{broker_symbol}") from exc
        if not (bid >= 0 and ask >= bid):
            raise MalformedBridgeQuote(f"MALFORMED_BRIDGE_QUOTE:{broker_symbol}")
        source_timestamp = raw.get("timestamp") or raw.get("time")
        if not source_timestamp:
            age_ms = float(raw.get("quote_age_ms") or 0)
            observed_at = datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)
            source_timestamp = observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return MarketQuote(instrument=instrument, bid=bid, ask=ask, source_timestamp=_normalize_source_timestamp(source_timestamp),
                          provider_id=self.provider_id, feed_id=raw.get("feed_id"),
                          data_status="FORWARD")

    def bars(self, instrument: str, *, timeframe: str = "M5") -> BarWindow | None:
        broker_symbol = resolve_broker_symbol(instrument)
        try:
            raw = self.client.rates(broker_symbol, timeframe, limit=1)
        except Exception as exc:
            from contracts.mt5_bridge.errors import BridgeReadTimeout, BridgeToolError, BridgeTransportError
            if isinstance(exc, BridgeReadTimeout):
                raise BridgeReadFailed(f"BRIDGE_TIMEOUT:{exc}") from exc
            if isinstance(exc, BridgeToolError) and re.search(r"symbol|instrument|not found|unknown", str(exc), re.I):
                raise BrokerSymbolUnavailable(f"BROKER_SYMBOL_UNAVAILABLE:{broker_symbol}:{exc}") from exc
            if isinstance(exc, (BridgeToolError, BridgeTransportError)):
                raise QuoteUnavailable(f"BARS_UNAVAILABLE:{broker_symbol}:{exc}") from exc
            raise BridgeReadFailed(f"BRIDGE_READ_FAILED:{broker_symbol}:{exc}") from exc
        rows = raw if isinstance(raw, list) else raw.get("rates") if isinstance(raw, dict) else None
        if not rows:
            return None
        last = rows[-1]
        completed_through = str(last.get("time") or last.get("timestamp") or "")
        digest = "sha256:" + hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        return BarWindow(timeframe=timeframe, completed_through=completed_through, count=len(rows), digest=digest)
