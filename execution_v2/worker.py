"""Orchestrates the full V2 execution lifecycle for one canonical EntrySignal (mission section 1
diagram): ExecutionEligibility -> ExecutionIntent -> ownership/generation acquisition (PostgreSQL,
`platform.acquire_ownership`, unmodified) -> broker-held fence issuance and validation
(`fence.py` + whatever `BridgeFence`-shaped boundary this worker was constructed with - in
production, `execution_v2.runtime.bridge_client.HttpBridgeFenceClient`, talking to the REAL
bridge over HTTP; `bridge_fence_sim.BridgeFenceSimulator` is test-only and this module never
imports it) -> one submission attempt -> ExecutionResult -> canonical PostgreSQL persistence +
JetStream event.

The single authority check (`execution_authority_enabled`) is evaluated *before* anything else
in `process_signal` and is the only thing that can make a broker effect possible - never an
EntrySignal's existence, a ManagedTrade, a PostgreSQL lease, or bridge reachability alone
(mission section 2).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction

from .bridge_fence_errors import (ExpiredGrant, InvalidSignature, RequestFingerprintMismatch,
                                  StaleGeneration, WrongAccount)
from .bridge_fence_types import BridgeFence
from .fence import FenceAuthority
from .ids import attempt_id as _attempt_id
from .ids import execution_result_id as _execution_result_id
from .intent import EntrySignalRecordMissing, IntentResult, create_execution_intent
from .risk import RiskPolicy, RiskPolicyError
from .symbols import (canonical_request_fingerprint, canonical_request_text, correlation_comment,
                      resolve_broker_symbol)

TOOL = "mt5_canonical_order_send"
RESULT_EVENT_TYPE = "execution.result.recorded.v1"

_TERMINAL_ATTEMPT_STATES = frozenset({"CONFIRMED", "REJECTED", "FAILED", "FENCED", "CANCELLED", "NOT_SENT"})


class ExecutionAuthorityDisabled(RuntimeError):
    """The single hard gate: nothing downstream of this may ever run while execution authority
    is not explicitly enabled."""


class OwnershipFenceFailure(RuntimeError):
    """A stale generation, expired grant, or any other independent bridge rejection at the
    fence-acquisition step (not at submission) - the attempt never reached SENDING."""


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str  # NO_OP_DISABLED | BLOCKED | FENCED_OUT | RECONCILIATION_REQUIRED | RESULT_RECORDED
    intent_result: IntentResult | None
    attempt_id: str | None
    result_outcome: str | None
    detail: str | None = None


def request_fingerprint(*, instrument: str, direction: str, volume: float, stop_price: float,
                        target_price: float | None) -> str:
    # Normalize numeric wire values exactly as the bridge's independent verifier does before
    # canonical JSON serialization.  This prevents an int/float spelling difference (4300 vs
    # 4300.0) from making an otherwise identical signed request unverifiable.
    payload = {"instrument": instrument, "direction": direction, "volume": float(volume),
              "stop_price": float(stop_price),
              "target_price": float(target_price) if target_price is not None else None}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


class ExecutionWorker:
    def __init__(self, conn: Any, *, fence_authority: FenceAuthority, bridge: BridgeFence,
                 holder_instance_id: str, account_id: str, mode: str, risk_policy: RiskPolicy,
                 risk_context_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 risk_policy_provider: Callable[[], RiskPolicy] | None = None,
                 authority_provider: Callable[[], str] | None = None) -> None:
        if mode not in ("demo", "real"):
            raise ValueError("mode must be 'demo' or 'real'")
        self.conn = conn
        self.fence_authority = fence_authority
        self.bridge = bridge
        self.holder_instance_id = holder_instance_id
        self.account_id = account_id
        self.mode = mode
        self.risk_policy = risk_policy
        self.risk_policy_provider = risk_policy_provider
        self.authority_provider = authority_provider
        self.risk_context_provider = risk_context_provider
        self.resource = f"execution:{mode}:{account_id}"

    def _acquire_canary_slot(self, idempotency_key: str) -> bool:
        if not self.risk_policy.enabled or self.risk_policy.canary_max_new_executions <= 0:
            return True
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT execution_v2.acquire_canary_slot(%s,%s,%s)",
                            (self.resource, idempotency_key, self.risk_policy.canary_max_new_executions))
                row = cur.fetchone()
        return bool(row and row[0])

    def _read_generation(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT generation FROM platform.ownership_leases WHERE lease_key=%s", (self.resource,))
            row = cur.fetchone()
        return row[0] if row else 0

    def _acquire_generation(self) -> int:
        expected = self._read_generation()
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT platform.acquire_ownership(%s,%s,%s)",
                           (self.resource, self.holder_instance_id, expected))
                return cur.fetchone()[0]

    def _load_attempt(self, execution_intent_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT attempt_id, state, generation, account_id
                          FROM execution_v2.execution_attempt WHERE execution_intent_id=%s""",
                       (execution_intent_id,))
            row = cur.fetchone()
        return dict(zip(("attempt_id", "state", "generation", "account_id"), row)) if row else None

    def _claim_attempt(self, *, execution_intent_id: str, attempt_id: str, generation: int) -> dict[str, Any]:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO execution_v2.execution_attempt
                    (attempt_id, execution_intent_id, account_id, resource, generation, state)
                    VALUES (%s,%s,%s,%s,%s,'CLAIMED')
                    ON CONFLICT (execution_intent_id) DO NOTHING""",
                           (attempt_id, execution_intent_id, self.account_id, self.resource, generation))
        existing = self._load_attempt(execution_intent_id)
        assert existing is not None
        return existing

    def _set_attempt_state(self, attempt_id: str, state: str, *, sending: bool = False, terminal: bool = False) -> None:
        with self.conn.cursor() as cur:
            if sending:
                cur.execute("UPDATE execution_v2.execution_attempt SET state=%s, sending_at=now() WHERE attempt_id=%s",
                           (state, attempt_id))
            elif terminal:
                cur.execute("UPDATE execution_v2.execution_attempt SET state=%s, terminal_at=now() WHERE attempt_id=%s",
                           (state, attempt_id))
            else:
                cur.execute("UPDATE execution_v2.execution_attempt SET state=%s WHERE attempt_id=%s", (state, attempt_id))

    def _persist_result(self, *, attempt_id: str, execution_intent_id: str, intent: dict[str, Any],
                        outcome: str, attempt_terminal_state: str,
                        broker_response: dict[str, Any] | None) -> None:
        result_id = _execution_result_id(attempt_id=attempt_id)
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        broker_response = broker_response or {}
        with transaction(self.conn):
            self._set_attempt_state(attempt_id, attempt_terminal_state, terminal=True)
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO execution_v2.execution_result
                    (execution_result_id, attempt_id, execution_intent_id, outcome, account_id,
                     broker_order_id, broker_deal_id, symbol, volume, requested_price, actual_price,
                     submitted_at, confirmed_at, raw_broker_evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (attempt_id) DO NOTHING""",
                           (result_id, attempt_id, execution_intent_id, outcome, self.account_id,
                            broker_response.get("broker_order_id"), broker_response.get("broker_deal_id"),
                            intent["instrument"], intent["approved_volume"], intent["requested_entry_price"],
                            broker_response.get("actual_price"),
                            now if outcome != "BLOCKED" else None,
                            now if outcome in ("FILLED", "CONFIRMED") else None,
                            canonical_bytes(broker_response).decode("utf-8")))
                payload = {"execution_result_id": result_id, "attempt_id": attempt_id,
                          "execution_intent_id": execution_intent_id, "outcome": outcome,
                          "account_id": self.account_id, "symbol": intent["instrument"]}
                cur.execute("""INSERT INTO platform.outbox_events
                    (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                     schema_version, payload, occurred_at, correlation_id, causation_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT (event_id) DO NOTHING""",
                           (f"{attempt_id}:execution.result.recorded", RESULT_EVENT_TYPE, "execution_attempt",
                            attempt_id, 1, "event-envelope.v1", canonical_bytes(payload).decode("utf-8"),
                            now, execution_intent_id, None))

    def _load_intent(self, execution_intent_id: str) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT instrument, direction, approved_volume, stop_price, target_price,
                                 requested_entry_price
                          FROM execution_v2.execution_intent WHERE execution_intent_id=%s""",
                       (execution_intent_id,))
            row = cur.fetchone()
        keys = ("instrument", "direction", "approved_volume", "stop_price", "target_price", "requested_entry_price")
        return dict(zip(keys, row))

    def process_signal(self, signal_id: str, *, execution_authority_enabled: bool,
                       broker_call: Callable[[], dict[str, Any]], now_utc: datetime | None = None) -> ExecutionOutcome:
        # `broker_call` has no default: mission section 8 forbids manufacturing FILLED from
        # request success alone, and an implicit "always FILLED" fallback would do exactly that
        # for any caller that forgot to wire a real/simulated broker boundary. Every caller -
        # every test and the production runtime consumer alike - must say explicitly what
        # "the broker" means for this call.
        if not execution_authority_enabled:
            raise ExecutionAuthorityDisabled("EXECUTION_AUTHORITY_MODE is not enabled")
        if self.authority_provider is not None and self.authority_provider() != "ENABLED":
            raise ExecutionAuthorityDisabled("PostgreSQL execution authority is not enabled")

        now_utc = now_utc or datetime.now(timezone.utc)
        if self.risk_policy_provider is not None:
            try:
                self.risk_policy = self.risk_policy_provider()
            except RiskPolicyError as exc:
                return ExecutionOutcome("BLOCKED", None, None, None, f"RISK_POLICY_UNAVAILABLE: {exc}")
        intent_result = create_execution_intent(self.conn, signal_id=signal_id, account_id=self.account_id,
                                                risk_policy=self.risk_policy, now_utc=now_utc,
                                                risk_context_provider=self.risk_context_provider)
        if intent_result.status == "QUARANTINED" or not intent_result.eligible:
            return ExecutionOutcome("BLOCKED", intent_result, None, None, intent_result.reason)

        execution_intent_id = intent_result.execution_intent_id
        intent = self._load_intent(execution_intent_id)
        att_id = _attempt_id(execution_intent_id=execution_intent_id)

        # Resolve the canonical instrument before claiming an execution attempt.  A missing
        # account-specific broker mapping is a deterministic configuration block, never a
        # partially claimed attempt or a guessed suffix.
        broker_symbol = resolve_broker_symbol(intent["instrument"], account_id=self.account_id, mode=self.mode)
        order_args = {
            "schema_version": 1, "action": 1, "magic": 0, "symbol": broker_symbol,
            "volume": float(intent["approved_volume"]),
            "price": float(intent["requested_entry_price"] or 0.0),
            "sl": float(intent["stop_price"]), "tp": float(intent["target_price"] or 0.0),
            "deviation": 50, "type": 0 if intent["direction"] == "LONG" else 1,
            "type_filling": 1, "type_time": 0, "expiration": 0,
            "comment": correlation_comment(att_id), "canonical_request_text": "",
            "request_fingerprint": "", "idempotency_key": att_id,
        }
        order_args["canonical_request_text"] = canonical_request_text(order_args)
        order_args["request_fingerprint"] = canonical_request_fingerprint(order_args)

        existing_attempt = self._load_attempt(execution_intent_id)
        if existing_attempt is not None and existing_attempt["state"] in _TERMINAL_ATTEMPT_STATES:
            with self.conn.cursor() as cur:
                cur.execute("SELECT outcome FROM execution_v2.execution_result WHERE attempt_id=%s", (att_id,))
                row = cur.fetchone()
            return ExecutionOutcome("RESULT_RECORDED", intent_result, att_id, row[0] if row else None,
                                    "attempt already terminal; no new broker effect")
        if existing_attempt is not None and existing_attempt["state"] == "UNCERTAIN":
            return ExecutionOutcome("RECONCILIATION_REQUIRED", intent_result, att_id, "UNCERTAIN",
                                    "prior attempt is UNCERTAIN; reconcile before any further action")

        try:
            generation = self._acquire_generation()
        except Exception as exc:
            return ExecutionOutcome("FENCED_OUT", intent_result, None, None, f"ownership acquisition failed: {exc}")

        if self.authority_provider is not None and self.authority_provider() != "ENABLED":
            return ExecutionOutcome("FENCED_OUT", intent_result, None, None,
                                    "execution authority was disabled before fencing")

        attempt = self._claim_attempt(execution_intent_id=execution_intent_id, attempt_id=att_id, generation=generation)
        att_id = attempt["attempt_id"]

        try:
            grant = self.fence_authority.mint_grant(resource=self.resource, generation=generation,
                                                     holder=self.holder_instance_id)
            self.bridge.advance_fence(grant)
        except (StaleGeneration, ExpiredGrant, InvalidSignature, WrongAccount) as exc:
            with transaction(self.conn):
                self._set_attempt_state(att_id, "FENCED", terminal=True)
            return ExecutionOutcome("FENCED_OUT", intent_result, att_id, None, f"fence advance rejected: {exc}")

        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT platform.assert_generation(%s,%s)", (self.resource, generation))
            self._set_attempt_state(att_id, "SENDING", sending=True)

        # Claim the durable slot immediately before the broker boundary. A conservative
        # reservation is intentional: an ambiguous/failed first attempt must never permit a
        # second blind live attempt.
        if not self._acquire_canary_slot(att_id):
            with transaction(self.conn):
                self._set_attempt_state(att_id, "FENCED", terminal=True)
            return ExecutionOutcome("BLOCKED", intent_result, att_id, None, "CANARY_LIMIT_EXCEEDED")

        fingerprint = request_fingerprint(instrument=broker_symbol, direction=intent["direction"],
                                          volume=float(intent["approved_volume"]), stop_price=float(intent["stop_price"]),
                                          target_price=float(intent["target_price"]) if intent["target_price"] is not None else None)
        authorization = self.fence_authority.mint_authorization(resource=self.resource, generation=generation,
                                                                 attempt_id=att_id, tool=TOOL,
                                                                 request_fingerprint=fingerprint)

        if self.authority_provider is not None and self.authority_provider() != "ENABLED":
            with transaction(self.conn):
                self._set_attempt_state(att_id, "FENCED", terminal=True)
            return ExecutionOutcome("FENCED_OUT", intent_result, att_id, None,
                                    "execution authority was disabled before broker submission")

        try:
            submit_result = self.bridge.submit(authorization=authorization, request_fingerprint=fingerprint,
                                               broker_call=broker_call, request_args=order_args)
        except (WrongAccount, InvalidSignature, RequestFingerprintMismatch) as exc:
            # The bridge's own independent verification rejected the request outright (never
            # reached broker_call) - this is exactly the OD-06 guarantee working as intended
            # (mission section 5: a fence/authorization the bridge does not accept must never
            # produce a broker effect, and must never crash the worker uncaught). Record the
            # attempt as terminally FENCED, matching the advance_fence-rejection path above.
            with transaction(self.conn):
                self._set_attempt_state(att_id, "FENCED", terminal=True)
            return ExecutionOutcome("FENCED_OUT", intent_result, att_id, None, f"bridge rejected submission: {exc}")

        if submit_result.state == "DISPATCHED":
            broker_response = submit_result.broker_response or {}
            broker_status = broker_response.get("status")
            if broker_status == "AMBIGUOUS":
                self._persist_result(attempt_id=att_id, execution_intent_id=execution_intent_id, intent=intent,
                                     outcome="UNKNOWN_RECONCILIATION_REQUIRED", attempt_terminal_state="UNCERTAIN",
                                     broker_response=broker_response)
                return ExecutionOutcome("RECONCILIATION_REQUIRED", intent_result, att_id,
                                        "UNKNOWN_RECONCILIATION_REQUIRED", "broker response lost/ambiguous")
            if broker_status == "REJECTED":
                self._persist_result(attempt_id=att_id, execution_intent_id=execution_intent_id, intent=intent,
                                     outcome="REJECTED", attempt_terminal_state="REJECTED",
                                     broker_response=broker_response)
                return ExecutionOutcome("RESULT_RECORDED", intent_result, att_id, "REJECTED", broker_response.get("reason"))
            outcome = "FILLED" if broker_status == "FILLED" else "SUBMITTED"
            self._persist_result(attempt_id=att_id, execution_intent_id=execution_intent_id, intent=intent,
                                 outcome=outcome, attempt_terminal_state="CONFIRMED", broker_response=broker_response)
            return ExecutionOutcome("RESULT_RECORDED", intent_result, att_id, outcome, None)

        if submit_result.state in ("CANCELLED_FENCED", "EXPIRED_BEFORE_DISPATCH"):
            self._persist_result(attempt_id=att_id, execution_intent_id=execution_intent_id, intent=intent,
                                 outcome="BLOCKED", attempt_terminal_state="FENCED" if submit_result.state == "CANCELLED_FENCED" else "NOT_SENT",
                                 broker_response={"bridge_state": submit_result.state})
            return ExecutionOutcome("FENCED_OUT", intent_result, att_id, "BLOCKED", submit_result.state)

        # Any other/unexpected bridge state: never assume success. Block for reconciliation.
        with transaction(self.conn):
            self._set_attempt_state(att_id, "UNCERTAIN", terminal=False)
        return ExecutionOutcome("RECONCILIATION_REQUIRED", intent_result, att_id, None,
                                f"unrecognized bridge ledger state: {submit_result.state}")
