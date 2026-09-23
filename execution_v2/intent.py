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
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction

from .ids import execution_intent_id as _execution_intent_id
from .risk import RiskPolicy

OUTBOX_EVENT_TYPE = "execution.intent.created.v1"


class EntrySignalRecordMissing(RuntimeError):
    """The EntrySignal referenced does not (yet) exist in strategy.entry_signals. Caller
    retries with backoff; never creates an intent from event payload alone (same contract as
    trade_management.managed_trade.EntrySignalRecordMissing)."""


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
    decision_time = record.get("decision_time")
    if decision_time is not None:
        decided = decision_time if isinstance(decision_time, datetime) else datetime.fromisoformat(str(decision_time).replace("Z", "+00:00"))
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=timezone.utc)
        age_seconds = (now_utc - decided).total_seconds()
        if age_seconds > risk_policy.max_signal_age_seconds:
            return EligibilityResult(False, "STALE_SIGNAL")
    return EligibilityResult(True, None)


def _load_entry_signal(conn: Any, signal_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT signal_id, strategy_id, strategy_version, strategy_ref, instrument,
                             direction, decision_time, entry_price, stop_price, target_price,
                             entry_signal_hash
                      FROM strategy.entry_signals WHERE signal_id=%s""", (signal_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("signal_id", "strategy_id", "strategy_version", "strategy_ref", "instrument",
            "direction", "decision_time", "entry_price", "stop_price", "target_price",
            "entry_signal_hash")
    return dict(zip(keys, row))


def _compute_volume(record: dict[str, Any], risk_policy: RiskPolicy) -> float:
    """Deliberately trivial for this first slice: the configured cap, never scaled by any
    per-signal quantity. Position sizing sophistication is explicitly out of scope (mission
    section 1 'Do NOT implement sophisticated position sizing')."""
    return risk_policy.max_volume


def create_execution_intent(conn: Any, *, signal_id: str, account_id: str, risk_policy: RiskPolicy,
                            now_utc: datetime, claimed_entry_signal_hash: str | None = None) -> IntentResult:
    with transaction(conn):
        record = _load_entry_signal(conn, signal_id)
        if record is None:
            raise EntrySignalRecordMissing(signal_id)
        if claimed_entry_signal_hash is not None and claimed_entry_signal_hash != record["entry_signal_hash"]:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO execution_v2.reconciliation_finding
                              (finding_id, attempt_id, broker_truth, detail)
                              VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING""",
                           (f"QUAR_{signal_id}", "NONE", "STILL_UNKNOWN",
                            canonical_bytes({"reason": "EVENT_HASH_MISMATCH", "signal_id": signal_id}).decode("utf-8")))
            return IntentResult(status="QUARANTINED", execution_intent_id=None, reason="EVENT_HASH_MISMATCH")

        eligibility = check_eligibility(record, risk_policy=risk_policy, account_id=account_id, now_utc=now_utc)
        intent_id = _execution_intent_id(entry_signal_id=signal_id, account_id=account_id)
        status = "PENDING" if eligibility.eligible else "BLOCKED"
        volume = _compute_volume(record, risk_policy) if eligibility.eligible else 0.0

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO execution_v2.execution_intent
                (execution_intent_id, entry_signal_id, entry_signal_hash, strategy_id, strategy_version,
                 strategy_ref, instrument, direction, order_type, requested_entry_price, stop_price,
                 target_price, approved_volume, risk_fraction, risk_policy_version, account_id, broker,
                 idempotency_key, status, block_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'MARKET',%s,%s,%s,%s,%s,%s,%s,'MT5',%s,%s,%s)
                ON CONFLICT (entry_signal_id) DO NOTHING
                RETURNING execution_intent_id""",
                       (intent_id, signal_id, record["entry_signal_hash"], record["strategy_id"],
                        record["strategy_version"], record["strategy_ref"], record["instrument"],
                        record["direction"], record["entry_price"], record["stop_price"], record["target_price"],
                        max(volume, 0.000001) if eligibility.eligible else 0.000001,  # CHECK (approved_volume > 0)
                        None,  # risk_fraction: reserved for future sizing sophistication (mission section 1
                               # "Do NOT implement sophisticated position sizing"); this slice uses a flat
                               # max_volume cap only (RiskPolicy has no fraction concept) - never computed here.
                        risk_policy.version if eligibility.eligible else None,
                        account_id, intent_id, status, eligibility.reason))
            inserted = cur.fetchone()

        if inserted is None:
            return IntentResult(status="DUPLICATE", execution_intent_id=intent_id,
                                eligible=eligibility.eligible, reason=eligibility.reason)

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
