from __future__ import annotations

import time
from typing import Any, Callable

from .store import TIMEFRAMES, MarketDataStore


def _completed(value: Any) -> list[dict[str, Any]]:
    rows = value.get("rates", []) if isinstance(value, dict) else []
    return rows[:-1] if len(rows) > 1 else []


class MarketDataCollector:
    """Small single-owner collector for the Redis market-data cache."""

    def __init__(self, store: MarketDataStore, read_tool: Callable[[str, dict[str, Any]], Any], *,
                 bar_symbols: Callable[[], list[str]], full_limit: int = 321,
                 quote_interval: float = 2.0, clock: Callable[[], float] = time.time):
        self.store, self.read_tool, self.bar_symbols = store, read_tool, bar_symbols
        self.full_limit, self.quote_interval, self.clock = full_limit, quote_interval, clock
        self.commands = 0

    def tick(self) -> dict[str, Any]:
        results = []
        now = self.clock()
        for symbol in self.bar_symbols():
            self.commands += 1
            try:
                snapshot = self.read_tool("mt5_symbol_snapshot", {
                    "symbol": symbol, "timeframes": list(TIMEFRAMES), "limit": self.full_limit,
                })
                rates = snapshot.get("rates") or {}
                if not snapshot.get("healthy") or snapshot.get("source_read_health") is not True:
                    raise RuntimeError("symbol snapshot unhealthy")
                if not isinstance(snapshot.get("symbol_info"), dict) or not isinstance(snapshot.get("quote"), dict):
                    raise RuntimeError("symbol snapshot missing metadata")
                for timeframe in TIMEFRAMES:
                    self.store.set_bars(symbol, timeframe, _completed(rates[timeframe]))
                self.store.set_snapshot(symbol, {"fetched_at": now, "symbol_info": snapshot["symbol_info"],
                                                 "quote": snapshot["quote"], "source_read_health": True})
                results.append({"symbol": symbol, "ok": True})
            except Exception as exc:
                results.append({"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        self.store.set_health({"updated_at": now, "status": "healthy" if all(r["ok"] for r in results) else "degraded",
                               "bridge_commands_total": self.commands,
                               "unhealthy_symbols": [r["symbol"] for r in results if not r["ok"]]})
        return {"bars": results, "failing": [r["symbol"] for r in results if not r["ok"]]}
