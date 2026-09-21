from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION


REASON_CODES = {
    "TOO_CLOSE_TO_RESISTANCE",
    "TOO_CLOSE_TO_SUPPORT",
    "HTF_CONTRADICTION",
    "EMA_CONFLICT",
    "SETUP_INVALIDATED_BEFORE_ENTRY",
    "RETRACE_TOO_DEEP",
    "NO_RETRACE",
    "SPREAD_TOO_HIGH",
    "STRUCTURE_CONFLICT",
    "NO_VALID_ENTRY",
}


def provenance(as_of: int, timeframe: str, source: str = "HISTORICAL_MT5") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "as_of_timestamp": int(as_of),
        "as_of_timeframe": timeframe,
        "completed_candles_only_for_events": True,
        "future_ohlc_exposed": False,
    }


def market_snapshot(symbol: str, as_of: int, quote: dict[str, Any] | None, contract: dict[str, Any] | None) -> dict[str, Any]:
    quote = quote or {}
    contract = contract or {}
    return {
        "schema": "market_snapshot",
        "symbol": symbol,
        "timestamp": int(as_of),
        "quote": {"bid": quote.get("bid"), "ask": quote.get("ask"), "time": quote.get("time")},
        "spread_price": (float(quote["ask"]) - float(quote["bid"])) if quote.get("ask") is not None and quote.get("bid") is not None else None,
        "spread_points": quote.get("spread_points", contract.get("spread_points")),
        "contract": contract,
        "provenance": provenance(as_of, "TICK", "HISTORICAL_MT5_BAR_AND_QUOTE"),
    }


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    required = {"schema", "symbol", "timestamp", "provenance"}
    missing = required - set(snapshot)
    if missing:
        raise ValueError(f"snapshot missing fields: {sorted(missing)}")
    if snapshot["provenance"].get("future_ohlc_exposed"):
        raise ValueError("snapshot provenance claims future OHLC exposure")
