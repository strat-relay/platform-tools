"""Market-data collector: the single owner of market-data reads on the read bridge (22347).

1. Completed bars. Per symbol, one `mt5_symbol_snapshot` when a new M5 bar can have closed (each
   5-minute boundary + a short grace), instead of on every strategy poll. The first fetch (or any
   detected gap/revision) is a full window (MARKET_DATA_FULL_LIMIT=321 rows -> 320 completed); every
   later one is a small window (MARKET_DATA_INCREMENTAL_LIMIT=8) merged under merge_completed()'s
   exact-overlap rules. If the forming M5 bar has not advanced (no tick yet / market closed) the
   symbol is retried a few times, then waits for the next boundary.
2. Metadata. Each snapshot's symbol_info is stored as metadata; extra symbols (not strategy
   members) are refreshed with `mt5_symbol_info` every MARKET_METADATA_REFRESH_SECONDS=300.
3. Quotes. Hot symbols (open trades and configured ones) are refreshed with `mt5_quote` every
   MARKET_QUOTE_REFRESH_SECONDS=2, capped per pass so quotes can never starve bar collection.

Failures never overwrite cached data; they update per-symbol state and collector health.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .store import TIMEFRAMES, BarGap, MarketDataStore, merge_completed

M5_SECONDS = 300


class SnapshotUnhealthy(RuntimeError):
    pass


def _completed(rates: Any) -> list[dict[str, Any]]:
    """Identical to the Context runner's _completed(): drop the forming (last) row."""
    rows = rates.get("rates", []) if isinstance(rates, dict) else []
    return rows[:-1] if len(rows) > 1 else []


def _forming_time(rates: Any) -> int | None:
    rows = rates.get("rates", []) if isinstance(rates, dict) else []
    return int(rows[-1]["time"]) if rows else None


class MarketDataCollector:
    def __init__(self, store: MarketDataStore, read_tool: Callable[[str, dict[str, Any]], Any], *,
                 bar_symbols: Callable[[], list[str]],
                 metadata_symbols: Callable[[], list[str]] = lambda: [],
                 quote_symbols: Callable[[], list[str]] = lambda: [],
                 full_limit: int = 321, incremental_limit: int = 8, boundary_grace: float = 2.0,
                 retry_seconds: float = 5.0, max_retries: int = 3,
                 metadata_interval: float = 300.0, quote_interval: float = 2.0, max_quotes_per_pass: int = 10,
                 clock: Callable[[], float] = time.time):
        self.store, self.read_tool = store, read_tool
        self.bar_symbols, self.metadata_symbols, self.quote_symbols = bar_symbols, metadata_symbols, quote_symbols
        self.full_limit, self.incremental_limit = full_limit, incremental_limit
        self.boundary_grace, self.retry_seconds, self.max_retries = boundary_grace, retry_seconds, max_retries
        self.metadata_interval, self.quote_interval, self.max_quotes_per_pass = metadata_interval, quote_interval, max_quotes_per_pass
        self.clock = clock
        self.commands = 0                      # bridge commands issued (observability / tests)
        self._metadata_at: dict[str, float] = {}
        self._quote_at: dict[str, float] = {}
        self._health: dict[str, Any] = {"process_heartbeat_at": None, "pass_started_at": None, "last_progress_at": None,
                                        "last_completed_pass_at": None, "in_pass": False, "current_symbol": None,
                                        "symbols_completed_in_pass": 0, "symbols_total_in_pass": 0,
                                        "status": "starting", "unhealthy_symbols": [], "bridge_commands_total": 0}

    def _publish(self, **changes: Any) -> None:
        """Write md:health. Every write is a heartbeat: it is issued between bridge calls, so its age
        is bounded by one bridge call however long a (catch-up) pass takes."""
        now = self.clock()
        self._health.update(changes, process_heartbeat_at=now, updated_at=now,
                            bridge_commands_total=self.commands)
        started = self._health["pass_started_at"]
        self._health["current_pass_duration"] = now - started if self._health["in_pass"] and started else None
        self.store.set_health(dict(self._health))

    def _read(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.commands += 1
        return self.read_tool(tool, arguments)

    # ---- 1. completed bars ------------------------------------------------------------------
    def bars_due(self, symbol: str, now: float) -> bool:
        state = self.store.state(symbol)
        fetched_at = state.get("fetched_at")
        if fetched_at is None or state.get("needs_full"):
            return True
        boundary = (now // M5_SECONDS) * M5_SECONDS
        if fetched_at < boundary + self.boundary_grace <= now:
            return True                      # a bar closed since the last fetch
        pending = state.get("awaiting_new_bar_retries", 0)
        return 0 < pending <= self.max_retries and now - fetched_at >= self.retry_seconds

    def fetch_bars(self, symbol: str) -> dict[str, Any]:
        now = self.clock()
        state = self.store.state(symbol)
        full = state.get("fetched_at") is None or bool(state.get("needs_full"))
        try:
            try:
                return self._fetch(symbol, now, full=full, previous=state)
            except BarGap as gap:
                return self._fetch(symbol, now, full=True, previous=state, reason=str(gap))
        except Exception as exc:
            # Cached bars are left exactly as they were; a gap that could not be refilled is
            # remembered so the next attempt is a full window.
            failures = int(state.get("consecutive_failures", 0)) + 1
            state.update(last_attempt_at=now, consecutive_failures=failures, healthy=False,
                         last_error=f"{type(exc).__name__}: {exc}"[:300],
                         needs_full=bool(state.get("needs_full")) or isinstance(exc, BarGap))
            self.store.set_state(symbol, state)
            return {"symbol": symbol, "ok": False, "error": state["last_error"]}

    def _fetch(self, symbol: str, now: float, *, full: bool, previous: dict[str, Any], reason: str = "") -> dict[str, Any]:
        limit = self.full_limit if full else self.incremental_limit
        snapshot = self._read("mt5_symbol_snapshot", {"symbol": symbol, "timeframes": list(TIMEFRAMES), "limit": limit})
        if not isinstance(snapshot, dict) or not snapshot.get("healthy") or snapshot.get("source_read_health") is not True:
            raise SnapshotUnhealthy("symbol snapshot unhealthy")
        rates, info, quote = snapshot.get("rates") or {}, snapshot.get("symbol_info"), snapshot.get("quote")
        if not isinstance(info, dict) or not isinstance(quote, dict) or any(tf not in rates for tf in TIMEFRAMES):
            raise SnapshotUnhealthy("symbol snapshot missing required component")
        merged, added = {}, {}
        for tf in TIMEFRAMES:
            fresh = _completed(rates[tf])
            if full:
                merged[tf], added[tf] = fresh, len(fresh)
            else:
                merged[tf], added[tf] = merge_completed(self.store.bars(symbol, tf), fresh)   # raises BarGap
        for tf in TIMEFRAMES:        # write only after every timeframe validated
            self.store.set_bars(symbol, tf, merged[tf])
        forming = {tf: _forming_time(rates[tf]) for tf in TIMEFRAMES}
        self.store.set_snapshot(symbol, {"fetched_at": now, "symbol_info": info, "quote": quote, "forming": forming,
                                         "source_read_health": True, "full": full})
        self.store.set_metadata(symbol, info, now)
        advanced = previous.get("forming", {}).get("M5") != forming["M5"]
        retries = 0 if advanced or full else int(previous.get("awaiting_new_bar_retries", 0)) + 1
        state = {"fetched_at": now, "last_attempt_at": now, "healthy": True, "consecutive_failures": 0,
                 "needs_full": False, "forming": forming, "awaiting_new_bar_retries": retries,
                 "last_full_at": now if full else previous.get("last_full_at"),
                 "last_full_reason": reason or (previous.get("last_full_reason") if not full else "COLD_START"),
                 "depth": {tf: len(merged[tf]) for tf in TIMEFRAMES}}
        self.store.set_state(symbol, state)
        return {"symbol": symbol, "ok": True, "full": full, "added": added, "reason": reason}

    # ---- 2. metadata -------------------------------------------------------------------------
    def refresh_metadata(self, now: float) -> list[str]:
        refreshed = []
        for symbol in self.metadata_symbols():
            meta = self.store.metadata(symbol)
            if meta and now - float(meta["observed_at"]) < self.metadata_interval:
                continue
            try:
                info = self._read("mt5_symbol_info", {"symbol": symbol})
            except Exception:
                continue          # stale metadata stays, and readers apply their own max age
            if isinstance(info, dict) and info:
                self.store.set_metadata(symbol, info, now)
                refreshed.append(symbol)
        return refreshed

    # ---- 3. quotes ---------------------------------------------------------------------------
    def refresh_quotes(self, now: float) -> list[str]:
        refreshed = []
        due = [s for s in dict.fromkeys(self.quote_symbols()) if now - self._quote_at.get(s, float("-inf")) >= self.quote_interval]
        due.sort(key=lambda s: self._quote_at.get(s, float("-inf")))          # least recently refreshed first
        for symbol in due[: self.max_quotes_per_pass]:
            self._quote_at[symbol] = now
            try:
                quote = self._read("mt5_quote", {"symbol": symbol})
            except Exception:
                continue
            if isinstance(quote, dict) and not quote.get("error"):
                self.store.set_quote(symbol, quote, now)
                refreshed.append(symbol)
        return refreshed

    # ---- scheduler ---------------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        """One pass: due bar snapshots first (strategy inputs), then quotes, then metadata."""
        now = self.clock()
        due = [s for s in self.bar_symbols() if self.bars_due(s, now)]
        self._publish(pass_started_at=now, in_pass=True, current_symbol=None,
                      symbols_completed_in_pass=0, symbols_total_in_pass=len(due))
        results = []
        for index, symbol in enumerate(due):
            self._publish(current_symbol=symbol)
            result = self.fetch_bars(symbol)
            results.append(result)
            progress = {"last_progress_at": self.clock()} if result["ok"] else {}
            self._publish(current_symbol=None, symbols_completed_in_pass=index + 1, **progress)
        quotes = self.refresh_quotes(now)
        metadata = self.refresh_metadata(now)
        failing = [r["symbol"] for r in results if not r["ok"]]
        states = {s: self.store.state(s) for s in self.bar_symbols()}
        unhealthy = sorted(s for s, st in states.items() if not st.get("healthy"))
        progress = {"last_progress_at": self.clock()} if quotes or metadata else {}
        self._publish(in_pass=False, current_symbol=None, last_completed_pass_at=self.clock(),
                      status="healthy" if not unhealthy else "degraded", unhealthy_symbols=unhealthy, **progress)
        return {"bars": results, "quotes": quotes, "metadata": metadata, "failing": failing}
