"""Cached risk gate: the execution hot path's replacement for synchronous bridge risk reads.

    Redis RiskSnapshot -> freshness + health -> local risk evaluation (evaluate_candidate, unchanged)
    -> atomic account-scoped reservation (Lua) -> existing authority/fence/idempotency path

It performs zero read-bridge calls. Missing, stale or untrustworthy state fails closed with an
explicit reason (RISK_STATE_UNAVAILABLE / RISK_STATE_STALE) and structured diagnostics; there is no
synchronous fallback to MT5. Existing broker positions are ordinary inputs: with the current policy
an occupied slot is MAX_CONCURRENT_POSITIONS_EXCEEDED, not RISK_STATE_UNAVAILABLE.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..risk import RiskDecision, RiskPolicy, evaluate_candidate
from .snapshot import HEALTHY, MalformedBrokerState, RiskSnapshot, position_open_risk
from .store import RedisRiskStateStore

RISK_STATE_UNAVAILABLE = "RISK_STATE_UNAVAILABLE"
RISK_STATE_STALE = "RISK_STATE_STALE"


def _env_seconds(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


@dataclass(frozen=True)
class FreshnessPolicy:
    """Maximum ages of each snapshot component, from the collector cadences:
    fast (equity/positions/orders) every 10 s -> 30 s max age (three missed cycles);
    history every 30 s -> 90 s; reference every 300 s -> 900 s."""
    positions_max_age: float = 30.0
    orders_max_age: float = 30.0
    equity_max_age: float = 30.0
    history_max_age: float = 90.0
    reference_max_age: float = 900.0
    reservation_ttl: float = 60.0

    @classmethod
    def from_env(cls) -> "FreshnessPolicy":
        return cls(positions_max_age=_env_seconds("RISK_POSITION_MAX_AGE_SECONDS", 30.0),
                   orders_max_age=_env_seconds("RISK_ORDER_MAX_AGE_SECONDS", 30.0),
                   equity_max_age=_env_seconds("RISK_EQUITY_MAX_AGE_SECONDS", 30.0),
                   history_max_age=_env_seconds("RISK_HISTORY_MAX_AGE_SECONDS", 90.0),
                   reference_max_age=_env_seconds("RISK_REFERENCE_MAX_AGE_SECONDS", 900.0),
                   reservation_ttl=_env_seconds("RISK_RESERVATION_TTL_SECONDS", 60.0))


@dataclass
class GateResult:
    decision: RiskDecision
    context: dict[str, Any] | None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    reserved: bool = False           # this call created the reservation (caller releases on duplicate)


class RiskContextFailure(Exception):
    def __init__(self, reason: str, stage: str, code: str, retryable: bool, detail: str = ""):
        super().__init__(detail or code)
        self.reason, self.stage, self.code, self.retryable = reason, stage, code, retryable


def _age_ms(now: float, stamp: float | None) -> int | None:
    return None if stamp is None else int((now - stamp) * 1000)


class CachedRiskGate:
    def __init__(self, store: RedisRiskStateStore, *, broker_symbol_for: Callable[[str], str],
                 freshness: FreshnessPolicy | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.broker_symbol_for = broker_symbol_for
        self.freshness = freshness or FreshnessPolicy()
        self.clock = clock

    # ---- validation ----------------------------------------------------------------------
    def _usable_snapshot(self, now: float) -> RiskSnapshot:
        try:
            snapshot = self.store.read_snapshot()
        except MalformedBrokerState as exc:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, exc.stage, exc.code, False, str(exc)) from exc
        except Exception as exc:  # Redis unreachable
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "SNAPSHOT_READ", "REDIS_UNAVAILABLE", True, str(exc)) from exc
        if snapshot is None:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "SNAPSHOT_READ", "SNAPSHOT_MISSING", True)
        # Source health: a component whose latest collection failed is not trusted even while young.
        for component in ("fast", "history"):
            if snapshot.source_health.get(component) != HEALTHY:
                raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "SOURCE_HEALTH",
                                         f"{component.upper()}_SOURCE_{str(snapshot.source_health.get(component)).upper()}", True)
        f = self.freshness
        for label, stamp, limit in (("POSITIONS", snapshot.positions_observed_at, f.positions_max_age),
                                    ("ORDERS", snapshot.orders_observed_at, f.orders_max_age),
                                    ("EQUITY", snapshot.equity_observed_at, f.equity_max_age),
                                    ("HISTORY", snapshot.history_observed_at, f.history_max_age)):
            if stamp is None:
                raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "FRESHNESS", f"{label}_NEVER_OBSERVED", True)
            if now - stamp > limit:
                raise RiskContextFailure(RISK_STATE_STALE, "FRESHNESS", f"{label}_STALE", True)
        today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        if snapshot.trading_day != today:
            raise RiskContextFailure(RISK_STATE_STALE, "FRESHNESS", "HISTORY_FOR_PREVIOUS_DAY", True)
        if snapshot.equity is None or snapshot.daily_loss is None:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "SNAPSHOT_VALIDATION", "CRITICAL_VALUE_MISSING", False)
        return snapshot

    def _reference(self, references: dict[str, dict[str, Any]], provider_symbol: str, now: float) -> dict[str, Any]:
        reference = references.get(provider_symbol)
        if reference is None:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "REFERENCE", "SYMBOL_REFERENCE_MISSING", True)
        if now - float(reference.get("observed_at", 0)) > self.freshness.reference_max_age:
            raise RiskContextFailure(RISK_STATE_STALE, "REFERENCE", "SYMBOL_REFERENCE_STALE", True)
        return reference

    # ---- evaluation + reservation ---------------------------------------------------------
    def evaluate_and_reserve(self, record: dict[str, Any], *, policy: RiskPolicy, account_id: str,
                             intent_id: str, now_utc: datetime) -> GateResult:
        started = time.perf_counter()
        now = self.clock()
        diagnostics: dict[str, Any] = {"risk_context_source": "REDIS", "account_ref": self.store.ref}
        try:
            for _ in range(2):   # one retry if the collector published a new generation mid-evaluation
                result = self._attempt(record, policy=policy, account_id=account_id, intent_id=intent_id,
                                       now_utc=now_utc, now=now, diagnostics=diagnostics)
                if result is not None:
                    return result
                now = self.clock()
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "RESERVATION", "SNAPSHOT_CHURN", True)
        except RiskContextFailure as failure:
            diagnostics["risk_context_failure"] = {"stage": failure.stage, "code": failure.code,
                                                   "retryable": failure.retryable}
            diagnostics["risk_decision"] = "REJECTED"
            diagnostics["risk_rejection_reason"] = failure.reason
            return GateResult(RiskDecision(False, failure.reason), None, diagnostics)
        finally:
            diagnostics["risk_context_ms"] = round((time.perf_counter() - started) * 1000, 3)
            try:
                diagnostics["collector_health"] = self.store.read_health().get("status")
            except Exception:
                diagnostics["collector_health"] = "unknown"

    def _attempt(self, record: dict[str, Any], *, policy: RiskPolicy, account_id: str, intent_id: str,
                 now_utc: datetime, now: float, diagnostics: dict[str, Any]) -> GateResult | None:
        snapshot = self._usable_snapshot(now)
        references = self.store.read_references()
        try:
            snapshot_exposure = sum(position_open_risk(p, references.get(p.provider_symbol))
                                    for p in snapshot.open_positions)
        except MalformedBrokerState as exc:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, exc.stage, exc.code, True, str(exc)) from exc
        try:
            provider_symbol = self.broker_symbol_for(record["instrument"])
        except Exception as exc:
            raise RiskContextFailure(RISK_STATE_UNAVAILABLE, "REFERENCE", "SYMBOL_MAPPING_MISSING", False, str(exc)) from exc
        reference = self._reference(references, provider_symbol, now)
        active = self.store.active_reservations(snapshot, now)
        inflight = [r for r in active if r["status"] in ("RESERVED", "SUBMITTED", "UNKNOWN")]
        reserved_risk = sum(float(r["reserved_risk"]) for r in active)
        diagnostics.update({
            "snapshot_generation": snapshot.generation, "snapshot_age_ms": _age_ms(now, snapshot.observed_at),
            "positions_age_ms": _age_ms(now, snapshot.positions_observed_at),
            "orders_age_ms": _age_ms(now, snapshot.orders_observed_at),
            "equity_age_ms": _age_ms(now, snapshot.equity_observed_at),
            "history_age_ms": _age_ms(now, snapshot.history_observed_at),
            "open_position_count": snapshot.open_position_count, "pending_order_count": snapshot.pending_order_count,
            "active_reservation_count": len(active),
            "open_risk": None if math.isinf(snapshot_exposure) else round(snapshot_exposure, 6),
            "open_risk_unbounded": math.isinf(snapshot_exposure), "reserved_risk": round(reserved_risk, 6)})
        context = {
            "account": {"equity": float(snapshot.equity)},
            "broker": {k: float(reference[k]) for k in ("tick_size", "tick_value", "volume_min", "volume_max", "volume_step")},
            "state": {"daily_loss": float(snapshot.daily_loss),
                      "concurrent_positions": snapshot.open_position_count + len(active),
                      "concurrent_orders": snapshot.pending_order_count + len(inflight),
                      "account_exposure": snapshot_exposure + reserved_risk, "canary_used": 0},
        }
        decision = evaluate_candidate(record, policy=policy, account_id=account_id, now_utc=now_utc,
                                      broker=context["broker"], account=context["account"], state=context["state"])
        if not decision.permitted:
            diagnostics.update({"risk_decision": "REJECTED", "risk_rejection_reason": decision.reason})
            return GateResult(decision, context, diagnostics)
        outcome = self.store.reserve(
            intent_id=intent_id, signal_id=record["signal_id"], canonical_instrument=record["instrument"],
            direction=record["direction"], reserved_risk=float(decision.risk_amount or 0.0),
            expected_generation=snapshot.generation, ttl_seconds=self.freshness.reservation_ttl,
            max_positions=policy.max_concurrent_positions, max_orders=policy.max_concurrent_orders,
            max_exposure=policy.max_account_exposure, max_daily_loss=policy.max_daily_loss,
            snapshot_exposure=snapshot_exposure)
        if outcome.status == "RETRY":
            return None
        if outcome.status == "REJECTED":
            reason = outcome.reason or RISK_STATE_UNAVAILABLE
            diagnostics.update({"risk_decision": "REJECTED", "risk_rejection_reason": reason,
                                "reservation": "REFUSED_ATOMICALLY"})
            return GateResult(RiskDecision(False, reason), context, diagnostics)
        diagnostics.update({"risk_decision": "PERMITTED", "reservation": outcome.status})
        return GateResult(decision, context, diagnostics, reserved=outcome.status == "RESERVED")
