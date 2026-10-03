"""Risk-state collector: the only component that polls the read-only MT5 bridge for execution risk.

    MT5 read bridge -> normalize -> validate -> Redis RiskSnapshot / reference metadata

Collection classes (configurable):
    fast       equity/balance, open positions, pending orders   RISK_FAST_REFRESH_SECONDS=10
    history    today's realized deals (daily loss)              RISK_HISTORY_REFRESH_SECONDS=30
    reference  symbol sizing metadata                           RISK_REFERENCE_REFRESH_SECONDS=300

A failed collection never replaces the last valid data: it only marks that component's source
health (degraded, then unavailable) and records the failure, so execution fails closed on health
while the last known broker truth stays visible. A refresh request (after a fill, rejection or
other broker change) triggers an immediate fast collection; execution never waits for it.

Reservation reconciliation: SUBMITTED/UNKNOWN reservations only leave their capacity-holding state
when PostgreSQL (the durable execution record) shows a definite attempt outcome.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .snapshot import (DEGRADED, HEALTHY, UNAVAILABLE, MalformedBrokerState, daily_loss_from_history,
                       normalize_orders, normalize_positions, normalize_reference)
from .store import RedisRiskStateStore

FAILURES_BEFORE_UNAVAILABLE = 3
MAX_SOURCE_ERRORS = 20
# PostgreSQL attempt states that prove an order was / was not executed (worker.py semantics).
CONFIRMED_ATTEMPT_STATES = frozenset({"CONFIRMED"})
NOT_EXECUTED_ATTEMPT_STATES = frozenset({"REJECTED", "FENCED", "NOT_SENT", "CANCELLED", "FAILED"})
_LOG = logging.getLogger(__name__)


class RiskStateCollector:
    def __init__(self, store: RedisRiskStateStore, read_tool: Callable[[str, dict[str, Any]], Any], *,
                 canonical_for: Callable[[str], str | None],
                 reference_symbols: Callable[[], list[str]],
                 fast_interval: float = 10.0, history_interval: float = 30.0, reference_interval: float = 300.0,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.read_tool = read_tool
        self.canonical_for = canonical_for
        self.reference_symbols = reference_symbols
        self.intervals = {"fast": fast_interval, "history": history_interval, "reference": reference_interval}
        self.clock = clock
        self.health: dict[str, Any] = store.read_health() or {}
        self.health.setdefault("components", {})
        self._last_run: dict[str, float] = {}

    # ---- health ----------------------------------------------------------------------------
    def _component_health(self, component: str) -> dict[str, Any]:
        return self.health["components"].setdefault(component, {
            "status": UNAVAILABLE, "last_attempt_at": None, "last_success_at": None,
            "consecutive_failures": 0, "last_failure_stage": None, "last_failure_code": None})

    def _publish_health(self) -> None:
        statuses = [self._component_health(c)["status"] for c in ("fast", "history", "reference")]
        self.health["status"] = (HEALTHY if all(s == HEALTHY for s in statuses)
                                 else UNAVAILABLE if UNAVAILABLE in statuses[:2] else DEGRADED)
        self.health["updated_at"] = self.clock()
        self.store.write_health(self.health)

    def _succeeded(self, component: str, at: float) -> None:
        h = self._component_health(component)
        h.update(status=HEALTHY, last_attempt_at=at, last_success_at=at, consecutive_failures=0)

    def _failed(self, component: str, at: float, stage: str, code: str, message: str) -> None:
        h = self._component_health(component)
        failures = int(h["consecutive_failures"]) + 1
        status = UNAVAILABLE if failures >= FAILURES_BEFORE_UNAVAILABLE or h["last_success_at"] is None else DEGRADED
        h.update(status=status, last_attempt_at=at, consecutive_failures=failures,
                 last_failure_stage=stage, last_failure_code=code)

        def mark(snapshot):
            snapshot.source_health[component] = status
            snapshot.source_errors = (snapshot.source_errors + [
                {"component": component, "stage": stage, "code": code, "message": message[:200], "at": at}])[-MAX_SOURCE_ERRORS:]
            return snapshot
        self.store.write_snapshot(mark)   # data untouched: the last valid observation is kept

    def _run(self, component: str, collect: Callable[[float], None]) -> bool:
        started = self.clock()
        try:
            collect(started)
        except MalformedBrokerState as exc:
            self._failed(component, started, exc.stage, exc.code, str(exc))
            self._publish_health()
            return False
        except Exception as exc:  # bridge unreachable, timeouts, Redis write errors
            self._failed(component, started, "BRIDGE_READ", type(exc).__name__.upper(), str(exc))
            self._publish_health()
            return False
        self._succeeded(component, started)
        self._last_run[component] = started
        self._publish_health()
        return True

    # ---- collections -----------------------------------------------------------------------
    def collect_fast(self) -> bool:
        def collect(at: float) -> None:
            account = self.read_tool("mt5_account_info", {})
            positions = normalize_positions(self.read_tool("mt5_positions", {}), self.canonical_for)
            orders = normalize_orders(self.read_tool("mt5_orders", {}), self.canonical_for)
            if not isinstance(account, dict) or account.get("equity") is None:
                raise MalformedBrokerState("ACCOUNT", "EQUITY_MISSING", "broker account equity is missing")
            equity, balance = float(account["equity"]), account.get("balance")
            # Exposure needs sizing metadata for every open position's symbol; fetch it now for a
            # symbol the reference cycle has not seen yet, instead of waiting up to its interval.
            known = set(self.store.read_references())
            for symbol in sorted({p.provider_symbol for p in positions} - known):
                self.store.write_reference(symbol, normalize_reference(
                    self.read_tool("mt5_symbol_info", {"symbol": symbol}), symbol))

            def apply(snapshot):
                snapshot.equity, snapshot.balance = equity, float(balance) if balance is not None else None
                snapshot.open_positions, snapshot.pending_orders = positions, orders
                snapshot.equity_observed_at = snapshot.positions_observed_at = snapshot.orders_observed_at = at
                snapshot.source_health["fast"] = HEALTHY
                return snapshot
            self.store.write_snapshot(apply)
        return self._run("fast", collect)

    def collect_history(self) -> bool:
        def collect(at: float) -> None:
            day = datetime.fromtimestamp(at, tz=timezone.utc).date()
            realized, loss = daily_loss_from_history(self.read_tool("mt5_history", {"limit": 500}), day)

            def apply(snapshot):
                snapshot.trading_day, snapshot.daily_realized_pnl, snapshot.daily_loss = day.isoformat(), realized, loss
                snapshot.history_observed_at = at
                snapshot.source_health["history"] = HEALTHY
                return snapshot
            self.store.write_snapshot(apply)
        return self._run("history", collect)

    def collect_reference(self) -> bool:
        def collect(at: float) -> None:
            snapshot = self.store.read_snapshot()
            symbols = set(self.reference_symbols())
            symbols |= {p.provider_symbol for p in (snapshot.open_positions if snapshot else [])}
            ordered_symbols = sorted(symbols)
            _LOG.info("risk_reference_batch_started symbol_count=%d symbols=%s",
                      len(ordered_symbols), ordered_symbols)
            for index, symbol in enumerate(ordered_symbols, start=1):
                started = time.monotonic()
                _LOG.info("risk_reference_symbol_started index=%d/%d symbol=%s",
                          index, len(ordered_symbols), symbol)
                try:
                    payload = self.read_tool("mt5_symbol_info", {"symbol": symbol})
                    reference = normalize_reference(payload, symbol)
                    self.store.write_reference(symbol, reference)
                except Exception:
                    _LOG.exception("risk_reference_symbol_failed index=%d/%d symbol=%s elapsed_ms=%.1f",
                                   index, len(ordered_symbols), symbol, (time.monotonic() - started) * 1000)
                    raise
                _LOG.info("risk_reference_symbol_succeeded index=%d/%d symbol=%s elapsed_ms=%.1f",
                          index, len(ordered_symbols), symbol, (time.monotonic() - started) * 1000)

            def apply(snap):
                snap.source_health["reference"] = HEALTHY
                return snap
            self.store.write_snapshot(apply)
            _LOG.info("risk_reference_batch_succeeded symbol_count=%d elapsed_ms=%.1f",
                      len(ordered_symbols), (time.monotonic() - at) * 1000)
        return self._run("reference", collect)

    def tick(self) -> dict[str, bool]:
        """One scheduler pass: run each due collection (fast early on a refresh request)."""
        now, ran = self.clock(), {}
        refresh = self.store.take_refresh_request()
        for component, collect in (("reference", self.collect_reference), ("fast", self.collect_fast),
                                   ("history", self.collect_history)):
            due = now - self._last_run.get(component, float("-inf")) >= self.intervals[component]
            if due or (refresh and component in ("fast", "history")):
                ran[component] = collect()
        return ran

    # ---- reservation reconciliation --------------------------------------------------------
    def reconcile_reservations(self, attempt_states: Callable[[list[str]], dict[str, str]]) -> dict[str, str]:
        """Resolve SUBMITTED/UNKNOWN reservations from durable PostgreSQL attempt states only.
        Anything still in flight or UNCERTAIN keeps holding capacity."""
        held = {i: r for i, r in self.store.reservations().items() if r["status"] in ("SUBMITTED", "UNKNOWN")}
        changes: dict[str, str] = {}
        if held:
            states = attempt_states(sorted(held))
            for intent_id, reservation in held.items():
                state = states.get(intent_id)
                if state in CONFIRMED_ATTEMPT_STATES:
                    ok, _ = self.store.transition(intent_id, allowed_from=("SUBMITTED", "UNKNOWN"), to="CONFIRMED",
                                                  stamp_field="confirmed_at", note="RECONCILED_FROM_ATTEMPT")
                    if ok:
                        changes[intent_id] = "CONFIRMED"
                elif state in NOT_EXECUTED_ATTEMPT_STATES:
                    ok, _ = self.store.transition(intent_id, allowed_from=("SUBMITTED", "UNKNOWN"), to="RELEASED",
                                                  note=f"RECONCILED_ATTEMPT_{state}")
                    if ok:
                        changes[intent_id] = "RELEASED"
        self.store.prune(self.store.read_snapshot())
        return changes


def attempt_states_from_postgres(connect_fn: Callable[..., Any]) -> Callable[[list[str]], dict[str, str]]:
    def read(intent_ids: list[str]) -> dict[str, str]:
        with connect_fn(readonly=True) as conn, conn.cursor() as cur:
            cur.execute("""SELECT execution_intent_id, state FROM execution_v2.execution_attempt
                           WHERE execution_intent_id = ANY(%s)""", (intent_ids,))
            return {intent: state for intent, state in cur.fetchall()}
    return read
