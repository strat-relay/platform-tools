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

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable

from .store import TIMEFRAMES, TIMEFRAME_SECONDS, BarGap, MarketDataStore, merge_by_time, merge_completed, unexpected_missing_times
from .session_calendar import classify_missing, gap_payload
from .bridge_guard import (BridgeCircuitBreaker, BridgeCircuitOpen, BridgeRequestGate,
                           classify_bridge_failure)

M5_SECONDS = 300
RECOVERY_STATES = frozenset({"HEALTHY", "STALE", "GAP_DETECTED", "BACKFILL_REQUIRED", "BACKFILLING", "REPLAYING", "BACKFILL_FAILED"})
log = logging.getLogger("market_data_cache")


class SnapshotUnhealthy(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


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
                 quote_max_age: float | None = None,
                 clock: Callable[[], float] = time.time,
                 bridge_breaker: BridgeCircuitBreaker | None = None,
                 bridge_max_inflight: int | None = None):
        self.store, self.read_tool = store, read_tool
        self.bar_symbols, self.metadata_symbols, self.quote_symbols = bar_symbols, metadata_symbols, quote_symbols
        self.full_limit, self.incremental_limit = full_limit, incremental_limit
        self.boundary_grace, self.retry_seconds, self.max_retries = boundary_grace, retry_seconds, max_retries
        self.metadata_interval, self.quote_interval, self.max_quotes_per_pass = metadata_interval, quote_interval, max_quotes_per_pass
        self.quote_max_age = (float(os.getenv("MARKET_QUOTE_MAX_AGE_SECONDS", "15"))
                              if quote_max_age is None else quote_max_age)
        self.clock = clock
        self.bridge_breaker = bridge_breaker or BridgeCircuitBreaker(
            failure_threshold=int(os.getenv("MARKET_DATA_BRIDGE_CB_FAILURE_THRESHOLD", "3")),
            initial_backoff=float(os.getenv("MARKET_DATA_BRIDGE_CB_INITIAL_BACKOFF_SECONDS", "5")),
            max_backoff=float(os.getenv("MARKET_DATA_BRIDGE_CB_MAX_BACKOFF_SECONDS", "60")),
            half_open_probes=int(os.getenv("MARKET_DATA_BRIDGE_CB_HALF_OPEN_PROBES", "1")), clock=clock)
        self.bridge_gate = BridgeRequestGate(
            read_tool, self.bridge_breaker,
            max_inflight=bridge_max_inflight or int(os.getenv("MARKET_DATA_BRIDGE_MAX_INFLIGHT", "4")), clock=clock)
        self._recovery_snapshot_budget = 0
        self.commands = 0                      # bridge commands issued (observability / tests)
        self._metadata_at: dict[str, float] = {}
        self._quote_at: dict[str, float] = {}
        self._last_request: dict[str, Any] = {}
        self._health: dict[str, Any] = {"process_heartbeat_at": None, "pass_started_at": None, "last_progress_at": None,
                                        "last_completed_pass_at": None, "in_pass": False, "current_symbol": None,
                                        "symbols_completed_in_pass": 0, "symbols_total_in_pass": 0,
                                        "status": "starting", "unhealthy_symbols": [], "bridge_commands_total": 0}

    def _publish(self, **changes: Any) -> None:
        """Write md:health. Every write is a heartbeat: it is issued between bridge calls, so its age
        is bounded by one bridge call however long a (catch-up) pass takes."""
        now = self.clock()
        self._health.update(changes, **self.bridge_breaker.snapshot(),
                            bridge_requests_coalesced_total=self.bridge_gate.requests_coalesced_total,
                            bridge_inflight=self.bridge_gate.inflight_count,
                            bridge_max_inflight=self.bridge_gate.max_inflight,
                            process_heartbeat_at=now, updated_at=now,
                            bridge_commands_total=self.commands)
        started = self._health["pass_started_at"]
        self._health["current_pass_duration"] = now - started if self._health["in_pass"] and started else None
        self.store.set_health(dict(self._health))

    def _read(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.commands += 1
        self._last_request = {"operation": tool, **arguments}
        return self.bridge_gate.call(tool, arguments)

    def _probe_bridge(self) -> bool:
        started = time.perf_counter()
        try:
            self._read("mt5_terminal_info", {})
            log.info("%s", json.dumps({"event": "bridge_health_probe", "success": True,
                                        "latency_ms": (time.perf_counter() - started) * 1000}, sort_keys=True))
            return True
        except Exception as exc:
            log.warning("%s", json.dumps({"event": "bridge_health_probe", "success": False,
                                           "latency_ms": (time.perf_counter() - started) * 1000,
                                           "error": str(exc)[:300]}, sort_keys=True))
            return False

    def _failure_telemetry(self, symbol: str, state: dict[str, Any], exc: Exception) -> None:
        request = dict(self._last_request)
        cached_times = []
        for timeframe in TIMEFRAMES:
            try:
                cached_times.extend(int(row["time"]) for row in self.store.bars(symbol, timeframe))
            except (KeyError, TypeError, ValueError):
                continue
        fetched_at = state.get("fetched_at")
        now = self.clock()
        payload = {
            "event": "market_data_fetch_failed",
            "provider_symbol": symbol,
            "timeframe": request.get("timeframe", "ALL"),
            "operation": request.get("operation", "mt5_symbol_snapshot"),
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "bridge_status": getattr(exc, "status", None) or getattr(exc, "status_code", None),
            "mt5_error_code": getattr(exc, "mt5_error_code", None) or getattr(exc, "last_error", None),
            "requested_start": request.get("start_timestamp"),
            "requested_end": request.get("end_timestamp"),
            "requested_count": request.get("limit", request.get("page_size")),
            "last_cached_timestamp": max(cached_times) if cached_times else None,
            "latest_source_timestamp": None,
            "cache_age_seconds": (now - float(fetched_at)) if fetched_at is not None else None,
            "consecutive_symbol_failures": int(state.get("consecutive_failures", 0)),
        }
        if isinstance(exc, SnapshotUnhealthy) and exc.details:
            payload["snapshot_error"] = exc.details.get("error")
            payload["snapshot_components"] = exc.details.get("components")
        log.error("%s", json.dumps(payload, sort_keys=True, default=str))

    # ---- 1. completed bars ------------------------------------------------------------------
    def bars_due(self, symbol: str, now: float) -> bool:
        state = self.store.state(symbol)
        retry_at = state.get("recovery_next_attempt_at")
        if retry_at is not None and now < float(retry_at):
            return False
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
            except SnapshotUnhealthy:
                # A cold start still requires the atomic full window. Once a
                # cache exists, however, a large snapshot can fail transiently
                # while a small overlapping snapshot succeeds. Use that small
                # window to drive bounded range recovery; never replace a
                # cached series with the shallow response itself.
                if full and state.get("fetched_at") is not None:
                    return self._fetch(symbol, now, full=False, previous=state,
                                       reason="FULL_SNAPSHOT_UNHEALTHY_FALLBACK")
                raise
            except BarGap as gap:
                return self._fetch(symbol, now, full=True, previous=state, reason=str(gap))
        except Exception as exc:
            global_failure = classify_bridge_failure(exc)
            if global_failure or isinstance(exc, BridgeCircuitOpen):
                state = self.store.state(symbol)
                state.update(healthy=False, health_state="BRIDGE_UNAVAILABLE",
                             last_attempt_at=now, last_error=f"{global_failure or 'BRIDGE_CIRCUIT_OPEN'}: {exc}"[:300])
                self.store.set_state(symbol, state)
                return {"symbol": symbol, "ok": False, "global_failure": True,
                        "classification": global_failure or "BRIDGE_CIRCUIT_OPEN",
                        "reason": str(exc), "error": str(exc)}
            # Cached bars are left exactly as they were; a gap that could not be refilled is
            # remembered so the next attempt is a full window.
            current = self.store.state(symbol)
            failures = int(current.get("consecutive_failures", state.get("consecutive_failures", 0))) + 1
            state = {**current, **state}
            if isinstance(exc, BarGap):
                retry_count = int(current.get("recovery_retry_count", 0)) + 1
                backoff = min(300.0, 5.0 * (2 ** min(retry_count - 1, 6)))
                state.update(recovery_retry_count=retry_count,
                             recovery_next_attempt_at=now + backoff,
                             recovery_last_error=str(exc)[:300])
            state.update(last_attempt_at=now, consecutive_failures=failures, healthy=False,
                         last_error=f"{type(exc).__name__}: {exc}"[:300],
                         needs_full=bool(state.get("needs_full")) or isinstance(exc, BarGap))
            self.store.set_state(symbol, state)
            state["health_state"] = "BACKFILL_FAILED" if state.get("backfill_required") else "STALE"
            self.store.set_state(symbol, state)
            self._failure_telemetry(symbol, state, exc)
            return {"symbol": symbol, "ok": False, "full": full, "reason": state["last_error"], "error": state["last_error"]}

    def _fetch(self, symbol: str, now: float, *, full: bool, previous: dict[str, Any], reason: str = "") -> dict[str, Any]:
        limit = self.full_limit if full else self.incremental_limit
        snapshot = self._read("mt5_symbol_snapshot", {"symbol": symbol, "timeframes": list(TIMEFRAMES), "limit": limit})
        if not isinstance(snapshot, dict) or not snapshot.get("healthy") or snapshot.get("source_read_health") is not True:
            raise SnapshotUnhealthy("symbol snapshot unhealthy", snapshot if isinstance(snapshot, dict) else None)
        rates, info, quote = snapshot.get("rates") or {}, snapshot.get("symbol_info"), snapshot.get("quote")
        if not isinstance(info, dict) or not isinstance(quote, dict) or any(tf not in rates for tf in TIMEFRAMES):
            raise SnapshotUnhealthy("symbol snapshot missing required component", snapshot)
        merged, added = {}, {}
        recovery = {}
        for tf in TIMEFRAMES:
            fresh = _completed(rates[tf])
            if full:
                cached = self.store.bars(symbol, tf)
                if cached and fresh and int(fresh[-1]["time"]) > int(cached[-1]["time"]) + TIMEFRAME_SECONDS[tf]:
                    merged[tf], recovery[tf] = self._recover_range(symbol, tf, cached, fresh, previous)
                    added[tf] = max(0, len(merged[tf]) - len(cached))
                else:
                    merged[tf], added[tf] = fresh, len(fresh)
            else:
                try:
                    merged[tf], added[tf] = merge_completed(self.store.bars(symbol, tf), fresh)
                except BarGap as gap:
                    if "differs" in str(gap):
                        # A revision is not a range gap: force a full snapshot so the broker's
                        # corrected overlap replaces the cached candle safely.
                        raise
                    cached = self.store.bars(symbol, tf)
                    merged[tf], recovery[tf] = self._recover_range(symbol, tf, cached, fresh, previous)
                    added[tf] = max(0, len(merged[tf]) - len(cached))
            holes = unexpected_missing_times(merged[tf], tf)
            if holes:
                anchor = [row for row in self.store.bars(symbol, tf) if int(row["time"]) < holes[0]]
                if anchor and fresh:
                    merged[tf], recovery[tf] = self._recover_range(symbol, tf, [anchor[-1]], fresh, previous,
                                                                    start_override=holes[0])
        for tf in TIMEFRAMES:        # write only after every timeframe validated
            self.store.set_bars(symbol, tf, merged[tf])
        forming = {tf: _forming_time(rates[tf]) for tf in TIMEFRAMES}
        self.store.set_snapshot(symbol, {"fetched_at": now, "symbol_info": info, "quote": quote, "forming": forming,
                                         "source_read_health": True, "full": full})
        self.store.set_metadata(symbol, info, now)
        advanced = previous.get("forming", {}).get("M5") != forming["M5"]
        retries = 0 if advanced or full else int(previous.get("awaiting_new_bar_retries", 0)) + 1
        state = {"fetched_at": now, "last_attempt_at": now, "healthy": True, "health_state": "HEALTHY", "consecutive_failures": 0,
                 "needs_full": False, "forming": forming, "awaiting_new_bar_retries": retries,
                 "last_full_at": now if full else previous.get("last_full_at"),
                 "last_full_reason": reason or (previous.get("last_full_reason") if not full else "COLD_START"),
                 "depth": {tf: len(merged[tf]) for tf in TIMEFRAMES}, "recovery": recovery,
                 "backfill_required": False, "recovery_retry_count": 0,
                 "recovery_next_attempt_at": None, "recovery_last_error": None,
                 "recovery_fingerprint": None}
        self.store.set_state(symbol, state)
        previous_failures = int(previous.get("consecutive_failures", 0))
        if previous_failures:
            missing_interval = [item for item in recovery.values() if item.get("status") == "RECOVERED"]
            log.info("%s", json.dumps({
                "event": "market_data_fetch_recovered",
                "provider_symbol": symbol,
                "previous_failure_count": previous_failures,
                "latest_source_timestamp": forming.get("M5"),
                "backfill_required": bool(recovery),
                "missing_interval": missing_interval,
            }, sort_keys=True, default=str))
        result_reason = reason
        if not result_reason and any(item.get("status") == "WINDOW_FALLBACK" for item in recovery.values()):
            result_reason = "full window does not reach cached series; used overlapping full-window fallback"
        return {"symbol": symbol, "ok": True, "full": full, "added": added, "reason": result_reason}

    def _recover_range(self, symbol: str, timeframe: str, cached: list[dict[str, Any]],
                       fresh: list[dict[str, Any]], previous: dict[str, Any],
                       *, start_override: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Recover the exact missing interval, in <=500-bar pages, before committing it."""
        if not cached or not fresh:
            raise BarGap(f"{timeframe} range recovery has no bounded cache/source")
        step = TIMEFRAME_SECONDS[timeframe]
        start = int(start_override if start_override is not None else cached[-1]["time"] + step)
        end = int(fresh[-1]["time"]) + step
        expected = max(0, (end - start) // step)
        source_watermark = int(fresh[-1]["time"])
        fingerprint = hashlib.sha256(json.dumps({
            "provider": "MT5", "provider_symbol": symbol, "timeframe": timeframe,
            "gap_start": start, "gap_end": end, "source_watermark": source_watermark,
        }, sort_keys=True).encode()).hexdigest()
        recovery_state = {**previous, "recovery_fingerprint": fingerprint,
                          "healthy": False, "health_state": "GAP_DETECTED",
                          "backfill_required": True, "gap_start": start, "gap_end": end,
                          "missing_candle_count": expected}
        self.store.set_state(symbol, {**previous, "healthy": False, "health_state": "GAP_DETECTED",
                                       "backfill_required": True, "gap_start": start, "gap_end": end,
                                       "missing_candle_count": expected, "recovery_fingerprint": fingerprint})
        self.store.set_state(symbol, {**recovery_state, "health_state": "BACKFILL_REQUIRED"})
        self.store.set_state(symbol, {**recovery_state, "health_state": "BACKFILLING",
                                       "backfill_required": True, "gap_start": start, "gap_end": end,
                                       "missing_candle_count": expected, "backfill_cursor": start})
        recovered: list[dict[str, Any]] = []
        cursor = start
        page_span = step * 500
        while cursor < end:
            page_end = min(end, cursor + page_span)
            try:
                result = self._read("mt5_rates_range", {"symbol": symbol, "timeframe": timeframe,
                                                          "start_timestamp": cursor, "end_timestamp": page_end,
                                                          "page_size": 500, "completed_only": True})
            except Exception as exc:
                log.error("%s", json.dumps({"event": "market_data_gap_detected",
                                             "provider_symbol": symbol, "timeframe": timeframe,
                                             "gap_start": start, "gap_end": end,
                                             "classification": "SOURCE_UNAVAILABLE",
                                             "severity": "ERROR", "session_source": "MT5_RESEARCH_SESSION_CALENDAR",
                                             "source_watermark": source_watermark,
                                             "error": str(exc)[:300]}, sort_keys=True, default=str))
                # A bounded full snapshot remains a safe compatibility fallback only when it
                # overlaps the persisted series; it cannot hide a gap larger than that window.
                if start_override is None and int(fresh[0]["time"]) <= int(cached[-1]["time"]):
                    combined = merge_by_time(cached, fresh)
                    cached_by_time = {int(row["time"]): row for row in cached}
                    overlap_revisions = [row for row in fresh
                                         if int(row["time"]) <= int(cached[-1]["time"])
                                         and cached_by_time.get(int(row["time"])) != row]
                    if not overlap_revisions and not [t for t in unexpected_missing_times(combined, timeframe)
                                                      if start <= t < int(fresh[-1]["time"])]:
                        return combined, {"status": "WINDOW_FALLBACK", "start": start, "end": end,
                                          "requested": expected, "received": len(fresh), "holes": []}
                raise BarGap(f"{timeframe} range recovery unavailable; full window does not reach cached series") from exc
            rows = result.get("rates", []) if isinstance(result, dict) else result if isinstance(result, list) else []
            rows = sorted((dict(row) for row in rows if isinstance(row, dict)), key=lambda row: int(row["time"]))
            recovered.extend(rows)
            cursor = page_end
            self.store.set_state(symbol, {**recovery_state, "health_state": "BACKFILLING",
                                           "backfill_required": True, "gap_start": start, "gap_end": end,
                                           "missing_candle_count": expected, "backfill_cursor": cursor,
                                           "recovery_fingerprint": fingerprint})
        combined = merge_by_time(cached, recovered, fresh)
        expected_gaps, true_gaps = classify_missing(combined, timeframe, provider="MT5", provider_symbol=symbol)
        if expected_gaps:
            log.info("%s", json.dumps({"event": "market_data_gap_detected", "provider_symbol": symbol,
                                       "timeframe": timeframe, "classification": "EXPECTED_SESSION_CLOSURE",
                                       "severity": "INFO", "session_source": "MT5_RESEARCH_SESSION_CALENDAR",
                                       "gaps": [gap_payload(gap, timeframe) for gap in expected_gaps]}, sort_keys=True))
        if true_gaps:
            log.error("%s", json.dumps({"event": "market_data_gap_detected", "provider_symbol": symbol,
                                        "timeframe": timeframe, "classification": "TRUE_BAR_GAP",
                                        "severity": "ERROR", "session_source": "MT5_RESEARCH_SESSION_CALENDAR",
                                        "gaps": [gap_payload(gap, timeframe) for gap in true_gaps]}, sort_keys=True))
        holes = [gap.start for gap in true_gaps]
        bounded_holes = [t for t in holes if start <= t < int(fresh[-1]["time"])]
        if int(combined[-1]["time"]) < int(fresh[-1]["time"]) or bounded_holes:
            raise BarGap(f"{timeframe} range recovery left {len(bounded_holes)} internal holes")
        return combined, {"status": "RECOVERED", "start": start, "end": end,
                          "requested": expected, "received": len(recovered), "holes": bounded_holes,
                          "continuity_status": "HEALTHY", "expected_session_closures":
                          [gap_payload(gap, timeframe) for gap in expected_gaps],
                          "recovery_fingerprint": fingerprint}

    # ---- 2. metadata -------------------------------------------------------------------------
    def refresh_metadata(self, now: float) -> list[str]:
        refreshed = []
        for symbol in self.metadata_symbols():
            meta = self.store.metadata(symbol)
            if meta and now - float(meta["observed_at"]) < self.metadata_interval:
                continue
            try:
                info = self._read("mt5_symbol_info", {"symbol": symbol})
            except Exception as exc:
                self._failure_telemetry(symbol, self.store.state(symbol), exc)
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
            except Exception as exc:
                self._failure_telemetry(symbol, self.store.state(symbol), exc)
                continue
            if not isinstance(quote, dict) or quote.get("error"):
                continue
            try:
                quote_time = float(quote["time"])
            except (KeyError, TypeError, ValueError):
                log.warning("%s", json.dumps({"event": "market_quote_rejected", "provider_symbol": symbol,
                                               "reason": "missing_timestamp"}, sort_keys=True))
                continue
            quote_age = now - quote_time
            if quote_age < -5 or quote_age > self.quote_max_age:
                log.warning("%s", json.dumps({"event": "market_quote_rejected", "provider_symbol": symbol,
                                               "reason": "stale_timestamp" if quote_age >= 0 else "future_timestamp",
                                               "quote_time": quote_time, "quote_age_seconds": quote_age,
                                               "max_age_seconds": self.quote_max_age}, sort_keys=True))
                continue
            self.store.set_quote(symbol, quote, now)
            refreshed.append(symbol)
        return refreshed

    # ---- scheduler ---------------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        """One pass: due bar snapshots first (strategy inputs), then quotes, then metadata."""
        now = self.clock()
        if self.bridge_breaker.state != BridgeCircuitBreaker.CLOSED:
            if not self.bridge_breaker.probe_due():
                self._publish(in_pass=False, status="degraded", bridge_health_state="BRIDGE_UNAVAILABLE",
                              unhealthy_symbols=sorted(s for s in self.bar_symbols()
                                                       if not self.store.state(s).get("healthy")))
                return {"bars": [], "quotes": [], "metadata": [], "failing": [], "bridge_unavailable": True}
            if not self._probe_bridge():
                self._publish(in_pass=False, status="degraded", bridge_health_state="BRIDGE_UNAVAILABLE",
                              unhealthy_symbols=sorted(s for s in self.bar_symbols()
                                                       if not self.store.state(s).get("healthy")))
                return {"bars": [], "quotes": [], "metadata": [], "failing": [], "bridge_unavailable": True}
            self._recovery_snapshot_budget = 1
        due = [s for s in self.bar_symbols() if self.bars_due(s, now)]
        if self._recovery_snapshot_budget:
            due = due[:self._recovery_snapshot_budget]
        self._publish(pass_started_at=now, in_pass=True, current_symbol=None,
                      symbols_completed_in_pass=0, symbols_total_in_pass=len(due))
        results = []
        for index, symbol in enumerate(due):
            self._publish(current_symbol=symbol)
            result = self.fetch_bars(symbol)
            results.append(result)
            if self.bridge_breaker.state != BridgeCircuitBreaker.CLOSED:
                break
            progress = {"last_progress_at": self.clock()} if result["ok"] else {}
            self._publish(current_symbol=None, symbols_completed_in_pass=index + 1, **progress)
        if self.bridge_breaker.state != BridgeCircuitBreaker.CLOSED:
            quotes, metadata = [], []
        else:
            quotes = self.refresh_quotes(now)
            metadata = self.refresh_metadata(now)
        failing = [r["symbol"] for r in results if not r["ok"]]
        states = {s: self.store.state(s) for s in self.bar_symbols()}
        unhealthy = sorted(s for s, st in states.items() if not st.get("healthy"))
        progress = {"last_progress_at": self.clock()} if quotes or metadata else {}
        self._recovery_snapshot_budget = max(0, self._recovery_snapshot_budget - 1)
        self._publish(in_pass=False, current_symbol=None, last_completed_pass_at=self.clock(),
                      status="healthy" if not unhealthy and self.bridge_breaker.state == BridgeCircuitBreaker.CLOSED else "degraded",
                      unhealthy_symbols=unhealthy, **progress)
        return {"bars": results, "quotes": quotes, "metadata": metadata, "failing": failing}
