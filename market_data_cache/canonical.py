"""Canonical, read-only market-data contract for live strategy consumers.

The collector is the only component allowed to repair this data from MT5. Strategy
consumers read Redis and receive the same freshness, continuity, and recovery
metadata regardless of which strategy they run.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .reader import MarketDataUnavailable
from .store import MarketDataStore, TIMEFRAMES, unexpected_missing_times


@dataclass(frozen=True)
class CanonicalBars:
    bars: tuple[dict[str, Any], ...]
    latest_completed_timestamp: int | None
    fetched_at: float
    source: str
    cache_age_seconds: float
    continuity_status: str
    gap_status: str
    recovery_status: str
    candle_identities: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalSnapshot:
    symbol_info: dict[str, Any]
    quote: dict[str, Any]
    bars: dict[str, CanonicalBars]
    fetched_at: float
    source: str
    cache_age_seconds: float
    continuity_status: str
    gap_status: str
    recovery_status: str


def candle_identity(provider_symbol: str, timeframe: str, timestamp: int) -> str:
    payload = f"{provider_symbol}|{timeframe}|{int(timestamp)}".encode()
    return hashlib.sha256(payload).hexdigest()[:24]


class CanonicalMarketDataReader:
    """Shared Redis reader; never falls back to MCP."""

    SOURCE = "REDIS_CANONICAL_CACHE"

    def __init__(self, store: MarketDataStore, *, max_age: float = 330.0, clock: Any = time.time):
        self.store, self.max_age, self.clock = store, float(max_age), clock

    def _state(self, symbol: str) -> dict[str, Any]:
        # Small compatibility stores used by read-only adapters may only expose
        # snapshot/bars; absence of state means no additional gap assertion.
        state_reader = getattr(self.store, "state", None)
        return state_reader(symbol) if state_reader is not None else {}

    def get_bars(self, canonical_instrument: str, provider_symbol: str, timeframe: str,
                 required_history: int) -> CanonicalBars:
        del canonical_instrument  # retained in the public contract for mapping/audit symmetry
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported canonical timeframe: {timeframe}")
        snapshot = self.store.snapshot(provider_symbol)
        state = self._state(provider_symbol)
        if snapshot is None:
            raise MarketDataUnavailable(f"canonical market data missing for {provider_symbol}")
        fetched_at = float(snapshot.get("fetched_at", 0))
        age = max(0.0, self.clock() - fetched_at)
        if age > self.max_age:
            raise MarketDataUnavailable(f"canonical market data for {provider_symbol} is stale ({age:.0f}s)")
        rows = self.store.bars(provider_symbol, timeframe)
        if len(rows) < required_history:
            raise MarketDataUnavailable(
                f"canonical market data for {provider_symbol} {timeframe} has {len(rows)} bars, needs {required_history}")
        selected = tuple(rows[-required_history:] if required_history else [])
        holes = unexpected_missing_times(list(selected), timeframe)
        state_health = str(state.get("health_state") or "HEALTHY").upper()
        continuity = "GAP" if holes or state.get("backfill_required") else "CONTINUOUS"
        gap = "GAP_DETECTED" if holes or state.get("backfill_required") else "NONE"
        recovery = state_health if state_health in {"BACKFILLING", "REPLAYING", "BACKFILL_FAILED", "GAP_DETECTED"} else "NONE"
        if gap != "NONE":
            raise MarketDataUnavailable(f"canonical market data for {provider_symbol} {timeframe} has gap state {gap}")
        return CanonicalBars(
            bars=selected,
            latest_completed_timestamp=int(selected[-1]["time"]) if selected else None,
            fetched_at=fetched_at,
            source=self.SOURCE,
            cache_age_seconds=age,
            continuity_status=continuity,
            gap_status=gap,
            recovery_status=recovery,
            candle_identities=tuple(candle_identity(provider_symbol, timeframe, int(row["time"])) for row in selected),
        )

    def get_snapshot(self, canonical_instrument: str, provider_symbol: str,
                     required_history: dict[str, int]) -> CanonicalSnapshot:
        raw = self.store.snapshot(provider_symbol)
        if raw is None:
            raise MarketDataUnavailable(f"canonical market data missing for {provider_symbol}")
        bars = {tf: self.get_bars(canonical_instrument, provider_symbol, tf, count)
                for tf, count in required_history.items()}
        ages = [item.cache_age_seconds for item in bars.values()]
        return CanonicalSnapshot(
            symbol_info=raw["symbol_info"], quote=raw["quote"], bars=bars,
            fetched_at=float(raw["fetched_at"]), source=self.SOURCE,
            cache_age_seconds=max(ages, default=0.0),
            continuity_status="CONTINUOUS", gap_status="NONE", recovery_status="NONE")


def canonical_health(store: MarketDataStore, provider_symbol: str, *, now: float | None = None,
                    max_age: float = 330.0) -> dict[str, Any]:
    """Expose one symbol-level health envelope for diagnostics and both runners."""
    now = time.time() if now is None else now
    state = store.state(provider_symbol)
    snapshot = store.snapshot(provider_symbol)
    fetched_at = float(snapshot["fetched_at"]) if snapshot and snapshot.get("fetched_at") is not None else None
    age = None if fetched_at is None else max(0.0, now - fetched_at)
    state_name = str(state.get("health_state") or "STALE").upper()
    if fetched_at is not None and age <= max_age and state.get("healthy", True) and state_name == "HEALTHY":
        state_name = "HEALTHY"
    elif age is not None and age > max_age:
        state_name = "STALE"
    return {
        "provider_symbol": provider_symbol, "source_watermark": state.get("forming", {}).get("M5"),
        "cache_watermark": max((int(row["time"]) for row in store.bars(provider_symbol, "M5")), default=None),
        "cache_age_seconds": age, "continuity": "CONTINUOUS" if not state.get("backfill_required") else "GAP",
        "gap": "NONE" if not state.get("backfill_required") else "GAP_DETECTED",
        "recovery": state_name if state_name in {"BACKFILLING", "REPLAYING", "BACKFILL_FAILED"} else "NONE",
        "state": state_name,
    }
