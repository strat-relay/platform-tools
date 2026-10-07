"""EntrySignal -> ExecutionIntent (mission section 3). Deterministic and idempotent: the same
canonical EntrySignal, consumed any number of times for the same personal account (duplicate
JetStream delivery, worker restart, retry of any kind), produces exactly one
`execution_v2.execution_intent` row - never a duplicate.

Follows the exact creation-transaction shape already proven in
`trade_management/managed_trade.py::create_managed_trade` (load by signal_id -> verify hash ->
insert idempotently -> outbox event in the same transaction) rather than inventing a new
pattern. Publishes to `platform.outbox_events` using the *existing* `execution.intent.created.v1`
subject (already registered, already mapped to the `EXECUTION` stream) - relayed by the same
generic `infrastructure.messaging.outbox_relay.OutboxRelay` the rest of this codebase already
uses; no new relay process is introduced for this event.

This module never decides whether execution authority is enabled - that check happens in
`worker.py`, strictly before anything here is even called, so an intent's mere existence never
implies permission to submit an order (mission section 2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction

from .ids import execution_intent_id as _execution_intent_id
from .risk import RiskPolicy, evaluate_candidate
from .risk_policy_store import policy_fingerprint
from .trace import emit as trace_emit

OUTBOX_EVENT_TYPE = "execution.intent.created.v1"
RESEARCH_ONLY_STRATEGIES = frozenset({"CONTEXT_STRUCTURE_RETRACE_V2"})


def _bridge_risk_diagnostics(risk_context: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe summary of the bridge risk context (an unbounded exposure is reported as such,
    not as a non-JSON infinity)."""
    account, state = risk_context.get("account", {}), risk_context.get("state", {})
    exposure = state.get("account_exposure")
    unbounded = isinstance(exposure, float) and exposure == float("inf")
    return {"sizing_basis": account.get("sizing_basis", "EQUITY"), "sizing_capital": account.get("sizing_capital"),
            "free_margin": account.get("free_margin"), "account_exposure": None if unbounded else exposure,
            "account_exposure_unbounded": unbounded, "positions": risk_context.get("positions")}


class EntrySignalRecordMissing(RuntimeError):
    """The EntrySignal referenced does not (yet) exist in strategy.entry_signals. Caller
    retries with backoff; never creates an intent from event payload alone (same contract as
    trade_management.managed_trade.EntrySignalRecordMissing)."""


class RetryableRiskState(RuntimeError):
    """Risk state is temporarily unavailable or stale; the event must be redelivered.

    This is used by the live consumer during collector warm-up or a short collector restart.
    No blocked intent is written, so a retry can evaluate the same signal after Redis has a
    fresh broker snapshot.
    """


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str | None = None


@dataclass(frozen=True)
class IntentResult:
    status: str  # CREATED | BLOCKED | DUPLICATE | QUARANTINED
    execution_intent_id: str | None
    eligible: bool = False
    reason: str | None = None


def check_eligibility(record: dict[str, Any], *, risk_policy: RiskPolicy, account_id: str,
                      now_utc: datetime) -> EligibilityResult:
    """Pure. No wall-clock side effect beyond the passed-in `now_utc`, no I/O - every reason a
    signal is blocked is enumerable and testable in isolation from PostgreSQL."""
    if not risk_policy.enabled:
        return EligibilityResult(False, "RISK_POLICY_DISABLED")
    if account_id not in risk_policy.allowed_accounts:
        return EligibilityResult(False, "ACCOUNT_NOT_ALLOWED")
    strategy_ref = record.get("strategy_ref") or f"{record.get('strategy_id')}@{record.get('strategy_version')}"
    if risk_policy.allowed_strategies and strategy_ref not in risk_policy.allowed_strategies:
        return EligibilityResult(False, "STRATEGY_NOT_ALLOWED")
    if risk_policy.allowed_symbols is not None and record["instrument"] not in risk_policy.allowed_symbols:
        return EligibilityResult(False, "SYMBOL_NOT_ALLOWED")
    if record.get("direction") not in ("LONG", "SHORT"):
        return EligibilityResult(False, "INVALID_DIRECTION")
    entry_price, stop_price = record.get("entry_price"), record.get("stop_price")
    if entry_price is None or stop_price is None:
        return EligibilityResult(False, "INVALID_GEOMETRY")
    entry_price, stop_price = float(entry_price), float(stop_price)
    if entry_price <= 0 or stop_price <= 0:
        return EligibilityResult(False, "INVALID_GEOMETRY")
    if record["direction"] == "LONG" and stop_price >= entry_price:
        return EligibilityResult(False, "INVALID_STOP_GEOMETRY")
    if record["direction"] == "SHORT" and stop_price <= entry_price:
        return EligibilityResult(False, "INVALID_STOP_GEOMETRY")
    target_price = record.get("target_price")
    if target_price is not None:
        target_price = float(target_price)
        if record["direction"] == "LONG" and target_price <= entry_price:
            return EligibilityResult(False, "INVALID_TARGET_GEOMETRY")
        if record["direction"] == "SHORT" and target_price >= entry_price:
            return EligibilityResult(False, "INVALID_TARGET_GEOMETRY")
    emitted_at = record.get("signal_emitted_at")
    if emitted_at is None:
        return EligibilityResult(False, "MISSING_SIGNAL_EMITTED_AT")
    emitted = emitted_at if isinstance(emitted_at, datetime) else datetime.fromisoformat(str(emitted_at).replace("Z", "+00:00"))
    if emitted.tzinfo is None:
        emitted = emitted.replace(tzinfo=timezone.utc)
    age_seconds = (now_utc - emitted).total_seconds()
    if age_seconds > risk_policy.max_signal_age_seconds:
        return EligibilityResult(False, "STALE_SIGNAL")
    return EligibilityResult(True, None)


def _load_entry_signal(conn: Any, signal_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT signal_id, strategy_id, strategy_version, strategy_ref, instrument,
                             direction, decision_time, signal_emitted_at, entry_price, stop_price, target_price,
                             entry_signal_hash
                      FROM strategy.entry_signals WHERE signal_id=%s""", (signal_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("signal_id", "strategy_id", "strategy_version", "strategy_ref", "instrument",
            "direction", "decision_time", "signal_emitted_at", "entry_price", "stop_price", "target_price",
            "entry_signal_hash")
    return dict(zip(keys, row))


def _compute_volume(record: dict[str, Any], risk_policy: RiskPolicy) -> float:
    """Deliberately trivial for this first slice: the configured cap, never scaled by any
    per-signal quantity. Position sizing sophistication is explicitly out of scope (mission
    section 1 'Do NOT implement sophisticated position sizing')."""
    return risk_policy.max_volume


def create_execution_intent(conn: Any, *, signal_id: str, account_id: str, risk_policy: RiskPolicy,
                            now_utc: datetime, claimed_entry_signal_hash: str | None = None,
                            risk_context_provider: Any | None = None,
                            blocked_reason: str | None = None,
                            risk_gate: Any | None = None,
                            retryable_risk_state: bool = False) -> IntentResult:
    started = time.perf_counter()
    trace_emit("INTENT_CREATION_STARTED", signal_id=signal_id, account_id=account_id)
    with transaction(conn):
        record = _load_entry_signal(conn, signal_id)
        if record is None:
            raise EntrySignalRecordMissing(signal_id)
        trace_emit("INTENT_SIGNAL_LOADED", signal_id=signal_id,
                   signal_emitted_at=record.get("signal_emitted_at"),
                   step_ms=round((time.perf_counter() - started) * 1000, 3))
        if claimed_entry_signal_hash is not None and claimed_entry_signal_hash != record["entry_signal_hash"]:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO execution_v2.reconciliation_finding
                              (finding_id, attempt_id, broker_truth, detail)
                              VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING""",
                           (f"QUAR_{signal_id}", "NONE", "STILL_UNKNOWN",
                            canonical_bytes({"reason": "EVENT_HASH_MISMATCH", "signal_id": signal_id}).decode("utf-8")))
            return IntentResult(status="QUARANTINED", execution_intent_id=None, reason="EVENT_HASH_MISMATCH")

        # Strategy-scoped boundary: research-only strategies must terminate before the
        # execution-intent insert, regardless of global authority or risk-policy state.
        # This is deliberately inside the canonical intent factory so no normal caller can
        # accidentally turn a V2 research signal into a durable executable record.
        if record.get("strategy_id") in RESEARCH_ONLY_STRATEGIES:
            trace_emit("INTENT_RESEARCH_STRATEGY_REJECTED", signal_id=signal_id,
                       strategy_id=record.get("strategy_id"), reason="STRATEGY_NOT_EXECUTION_ENABLED")
            return IntentResult(status="BLOCKED", execution_intent_id=None,
                                eligible=False, reason="STRATEGY_NOT_EXECUTION_ENABLED")

        eligibility = (EligibilityResult(False, blocked_reason) if blocked_reason else
                       check_eligibility(record, risk_policy=risk_policy, account_id=account_id, now_utc=now_utc))
        intent_id = _execution_intent_id(entry_signal_id=signal_id, account_id=account_id)
        status = "PENDING" if eligibility.eligible else "BLOCKED"
        volume = 0.0
        risk_fraction = None
        risk_context = None
        risk_decision = None
        diagnostics: dict[str, Any] = {}
        reserved_here = False
        if not blocked_reason and eligibility.eligible and risk_policy.risk_per_trade > 0 and risk_gate is not None:
            # RISK_CONTEXT_SOURCE=REDIS: cached snapshot + atomic reservation, no broker reads.
            gate_result = risk_gate.evaluate_and_reserve(record, policy=risk_policy, account_id=account_id,
                                                         intent_id=intent_id, now_utc=now_utc)
            risk_context, risk_decision = gate_result.context, gate_result.decision
            diagnostics, reserved_here = gate_result.diagnostics, gate_result.reserved
            if not risk_decision.permitted:
                if retryable_risk_state and diagnostics.get("risk_context_failure", {}).get("retryable"):
                    trace_emit("INTENT_RISK_STATE_RETRYABLE", signal_id=signal_id,
                               reason=risk_decision.reason, diagnostics=diagnostics)
                    raise RetryableRiskState(risk_decision.reason)
                eligibility = EligibilityResult(False, risk_decision.reason)
                status = "BLOCKED"
            else:
                volume = float(risk_decision.volume)
                risk_fraction = risk_policy.risk_per_trade
        elif not blocked_reason and eligibility.eligible and risk_policy.risk_per_trade > 0:
            if risk_context_provider is None:
                eligibility = EligibilityResult(False, "RISK_STATE_UNAVAILABLE")
                status = "BLOCKED"
            else:
                try:
                    risk_context = risk_context_provider(record)
                    diagnostics = _bridge_risk_diagnostics(risk_context)
                    risk_decision = evaluate_candidate(record, policy=risk_policy, account_id=account_id,
                                                       now_utc=now_utc, broker=risk_context["broker"],
                                                       account=risk_context["account"], state=risk_context["state"])
                    if not risk_decision.permitted:
                        eligibility = EligibilityResult(False, risk_decision.reason)
                        status = "BLOCKED"
                    else:
                        volume = float(risk_decision.volume)
                        risk_fraction = risk_policy.risk_per_trade
                except Exception as exc:
                    eligibility = EligibilityResult(False, "RISK_STATE_UNAVAILABLE")
                    status = "BLOCKED"
                    # Record why, so a rejection is explainable without the bridge journal.
                    diagnostics = {"risk_context_failure": {"stage": "BRIDGE_RISK_CONTEXT",
                                                            "error": f"{type(exc).__name__}: {exc}"[:300]}}
        elif eligibility.eligible:
            volume = _compute_volume(record, risk_policy)

        trace_emit("INTENT_ELIGIBILITY_COMPLETED", signal_id=signal_id, outcome=status,
                   reason=eligibility.reason,
                   risk_gate_enabled=risk_gate is not None,
                   step_ms=round((time.perf_counter() - started) * 1000, 3))

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO execution_v2.execution_intent
                (execution_intent_id, entry_signal_id, entry_signal_hash, strategy_id, strategy_version,
                 strategy_ref, instrument, direction, order_type, requested_entry_price, stop_price,
                 target_price, approved_volume, risk_fraction, risk_policy_version, account_id, broker,
                 idempotency_key, status, block_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'MARKET',%s,%s,%s,%s,%s,%s,%s,'MT5',%s,%s,%s)
                ON CONFLICT (entry_signal_id, account_id) DO NOTHING
                RETURNING execution_intent_id""",
                       (intent_id, signal_id, record["entry_signal_hash"], record["strategy_id"],
                        record["strategy_version"], record["strategy_ref"], record["instrument"],
                        record["direction"], record["entry_price"], record["stop_price"], record["target_price"],
                        max(volume, 0.000001) if eligibility.eligible else 0.000001,  # CHECK (approved_volume > 0)
                        risk_fraction,
                        # Always the policy version this decision was actually evaluated against -
                        # including a BLOCKED row. `risk_policy` is passed into this function
                        # unconditionally, so the value is already in scope for every outcome; there
                        # is no reason a rejection should lose policy provenance a later reviewer
                        # needs to reconstruct "what policy was in effect when this was rejected"
                        # (a rejected signal is exactly the case an operator most needs explained).
                        # risk_fraction stays legitimately None for a rejection that never computed
                        # one - only the policy identity itself is unconditional here.
                        risk_policy.version,
                        account_id, intent_id, status, eligibility.reason))
            inserted = cur.fetchone()

        trace_emit("INTENT_ROW_INSERTED", signal_id=signal_id, intent_id=intent_id,
                   inserted=inserted is not None, outcome=status,
                   step_ms=round((time.perf_counter() - started) * 1000, 3))

        if inserted is None:
            if reserved_here:
                # This evaluation reserved capacity for an intent that already existed; the
                # original evaluation owns that intent's lifecycle, so give the capacity back.
                risk_gate.store.release_before_submit(intent_id, "DUPLICATE_EVALUATION")
            return IntentResult(status="DUPLICATE", execution_intent_id=intent_id,
                                eligible=eligibility.eligible, reason=eligibility.reason)

        if risk_context is not None or diagnostics:
            risk_context = risk_context or {}
            if risk_context:
                broker = risk_context.get("broker", {})
                account = risk_context.get("account", {})
                state = risk_context.get("state", {})
                stop_distance = abs(float(record["entry_price"]) - float(record["stop_price"]))
                tick_size = float(broker.get("tick_size") or 0.0)
                tick_value = float(broker.get("tick_value") or 0.0)
                minimum_lot = float(broker.get("volume_min") or 0.0)
                minimum_lot_loss = (minimum_lot * stop_distance / tick_size * tick_value
                                    if tick_size > 0 and tick_value >= 0 else None)
                account_exposure = state.get("account_exposure")
                max_account_exposure = risk_policy.max_account_exposure
                remaining_exposure = (max(0.0, float(max_account_exposure) - float(account_exposure))
                                      if account_exposure is not None else None)
                diagnostics.update({
                    "account_exposure_usd": account_exposure,
                    "max_account_exposure_usd": max_account_exposure,
                    "remaining_account_exposure_usd": remaining_exposure,
                    "minimum_lot_estimated_loss_usd": minimum_lot_loss,
                    "projected_exposure_at_minimum_lot_usd": (
                        float(account_exposure) + minimum_lot_loss
                        if account_exposure is not None and minimum_lot_loss is not None else None),
                    "exposure_check": {
                        "comparison": "account_exposure_usd >= max_account_exposure_usd",
                        "blocked": (account_exposure is not None and
                                    float(account_exposure) >= float(max_account_exposure)),
                    },
                })
            evidence = {
                "execution_intent_id": intent_id,
                "policy_version": risk_policy.version,
                "policy_fingerprint": policy_fingerprint(risk_policy),
                "risk_per_trade": risk_policy.risk_per_trade,
                "account_equity": risk_context.get("account", {}).get("equity"),
                # The budget sizing actually used: equity, or free margin under FREE_MARGIN sizing.
                "risk_budget_usd": (float(_capital) * risk_policy.risk_per_trade
                                    if (_capital := risk_context.get("account", {}).get("sizing_capital",
                                                                                         risk_context.get("account", {}).get("equity"))) is not None
                                    else None),
                "stop_distance": abs(float(record["entry_price"]) - float(record["stop_price"])),
                "broker_volume_min": risk_context.get("broker", {}).get("volume_min"),
                "broker_volume_step": risk_context.get("broker", {}).get("volume_step"),
                "broker_volume_max": risk_context.get("broker", {}).get("volume_max"),
                "calculated_volume": risk_decision.volume if risk_decision else None,
                "submitted_volume": volume if eligibility.eligible else None,
                "estimated_loss_usd": risk_decision.risk_amount if risk_decision else None,
                "daily_loss_used": risk_context.get("state", {}).get("daily_loss"),
                "concurrent_positions_used": risk_context.get("state", {}).get("concurrent_positions"),
                "concurrent_orders_used": risk_context.get("state", {}).get("concurrent_orders"),
                "signal_age_seconds": (now_utc - (record["signal_emitted_at"] if isinstance(record["signal_emitted_at"], datetime)
                                                   else datetime.fromisoformat(str(record["signal_emitted_at"]).replace("Z", "+00:00")))).total_seconds(),
                "max_signal_age_seconds": risk_policy.max_signal_age_seconds,
                "canary_consumed": risk_context.get("state", {}).get("canary_used"),
                "canary_max": risk_policy.canary_max_new_executions,
                "decision_reason": eligibility.reason,
            }
            # Supplemental evidence must never roll back the authoritative intent or cause a
            # valid execution to be retried. A savepoint keeps a failed evidence insert isolated.
            try:
                with conn.cursor() as cur:
                    cur.execute("SAVEPOINT execution_risk_evidence")
                    cur.execute("""INSERT INTO execution_v2.execution_risk_evidence
                        (execution_intent_id, policy_version, policy_fingerprint, risk_per_trade,
                         account_equity, risk_budget_usd, stop_distance, broker_volume_min,
                         broker_volume_step, broker_volume_max, calculated_volume, submitted_volume,
                         estimated_loss_usd, daily_loss_used, concurrent_positions_used,
                         concurrent_orders_used, signal_age_seconds, max_signal_age_seconds,
                         canary_consumed, canary_max, decision_reason, diagnostics)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                        ON CONFLICT (execution_intent_id) DO NOTHING""",
                               (evidence["execution_intent_id"], evidence["policy_version"], evidence["policy_fingerprint"],
                                evidence["risk_per_trade"], evidence["account_equity"], evidence["risk_budget_usd"],
                                evidence["stop_distance"], evidence["broker_volume_min"], evidence["broker_volume_step"],
                                evidence["broker_volume_max"], evidence["calculated_volume"], evidence["submitted_volume"],
                                evidence["estimated_loss_usd"], evidence["daily_loss_used"], evidence["concurrent_positions_used"],
                                evidence["concurrent_orders_used"], evidence["signal_age_seconds"], evidence["max_signal_age_seconds"],
                                evidence["canary_consumed"], evidence["canary_max"], evidence["decision_reason"],
                                json.dumps(diagnostics, sort_keys=True, default=str)))
                    cur.execute("RELEASE SAVEPOINT execution_risk_evidence")
            except Exception:
                try:
                    with conn.cursor() as cur:
                        cur.execute("ROLLBACK TO SAVEPOINT execution_risk_evidence")
                        cur.execute("RELEASE SAVEPOINT execution_risk_evidence")
                except Exception:
                    pass

        payload = {"execution_intent_id": intent_id, "entry_signal_id": signal_id,
                  "strategy_ref": record["strategy_ref"], "instrument": record["instrument"],
                  "direction": record["direction"], "account_id": account_id, "status": status,
                  "eligible": eligibility.eligible, "reason": eligibility.reason}
        occurred_at = now_utc.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 schema_version, payload, occurred_at, correlation_id, causation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING""",
                       (f"{intent_id}:execution.intent.created", OUTBOX_EVENT_TYPE, "execution_intent",
                        intent_id, 1, "event-envelope.v1", canonical_bytes(payload).decode("utf-8"),
                        occurred_at, signal_id, None))

        return IntentResult(status="CREATED" if eligibility.eligible else "BLOCKED", execution_intent_id=intent_id,
                            eligible=eligibility.eligible, reason=eligibility.reason)
