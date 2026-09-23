"""`RealBridgeFenceBoundary`: the real (durable, independently-verifying) bridge-side OD-06
enforcement point. Same public contract as `execution_v2.bridge_fence_sim.BridgeFenceSimulator`
(`advance_fence`, `submit`, `ledger_entry`, `restart`, `.bridge_epoch`) so `execution_v2.worker.
ExecutionWorker` and `execution_v2.reconcile.reconcile_attempt` need no changes to use this
instead - only `store.py`'s durable SQLite backs it, not an in-memory dict.

Two-phase durable write around the broker call (mission section 7, the crash window): `submit()`
persists `SUBMISSION_IN_PROGRESS` BEFORE calling `broker_call()`, and only persists `DISPATCHED`
(with the broker's response) AFTER it returns. A crash at any point between those two writes -
including the worst case, broker_call() having actually reached MT5 - leaves the ledger at
`SUBMISSION_IN_PROGRESS`; `restart()`'s sweep converts exactly that state to
`UNCERTAIN_AFTER_RESTART`, never silently to success and never eligible for blind retry (the
idempotency check at the top of `submit()` returns whatever is on disk, unconditionally, before
any verification or broker call is attempted again). A row that already reached `DISPATCHED`
before a restart is left untouched - it already carries the broker's own fsync'd response, so it
is a settled fact, not something a restart should cast doubt on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from execution_v2.bridge_fence_errors import (ExpiredGrant, RequestFingerprintMismatch,
                                              StaleGeneration, WrongAccount)
from execution_v2.bridge_fence_types import AdvanceResult, SubmitResult
from execution_v2.bridge_fence_verify import verify_or_raise as _verify
from execution_v2.fence import FenceGrant, WriteAuthorization

from .store import FenceStore

_SETTLED_LEDGER_STATES = ("DISPATCHED", "CANCELLED_FENCED", "EXPIRED_BEFORE_DISPATCH",
                          "UNCERTAIN_AFTER_RESTART", "SUBMISSION_IN_PROGRESS")


class RealBridgeFenceBoundary:
    """`keys` is the bridge's own out-of-band-provisioned copy of the fence signing key material
    (never received over the platform's own request path in the real deployment).
    `configured_account_id` is the one account this bridge process instance is authorized to
    serve - an authorization naming any other account is independently rejected regardless of an
    otherwise-valid signature/generation/expiry. `db_path` is this boundary's own durable store,
    entirely separate from the platform's PostgreSQL."""

    def __init__(self, *, keys: dict[str, bytes], configured_account_id: str, db_path: str,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.keys = keys
        self.configured_account_id = configured_account_id
        self.clock = clock
        self.store = FenceStore(db_path)

    @property
    def bridge_epoch(self) -> int:
        return self.store.get_bridge_epoch()

    def restart(self) -> list[str]:
        """Simulates (and, once ported to the real bridge process, IS) an actual process
        restart: closes and reopens the durable store file, increments the persisted epoch, and
        sweeps every genuinely in-flight ledger entry (SUBMISSION_IN_PROGRESS - the pre-broker-
        call durable write with no matching post-call write yet) to UNCERTAIN_AFTER_RESTART. A
        row that already reached DISPATCHED before the restart is left as DISPATCHED: it already
        carries the broker's own recorded response, fsync'd before this call, so it is a settled
        fact. Returns the swept attempt_ids for test/audit visibility."""
        self.store.reopen()
        self.store.increment_bridge_epoch()
        return self.store.sweep_in_progress_to_uncertain()

    def advance_fence(self, grant: FenceGrant) -> AdvanceResult:
        _verify(self.keys, grant.signed_payload(), grant.sig, grant.key_id)
        now = self.clock()
        not_after = datetime.fromisoformat(grant.not_after)
        if now > not_after:
            raise ExpiredGrant(f"grant for {grant.resource} expired at {grant.not_after} (now={now.isoformat()})")

        current = self.store.get_fence_state(grant.resource)
        current_generation = current["generation"] if current else 0
        if grant.generation < current_generation:
            raise StaleGeneration(f"grant generation {grant.generation} < current {current_generation} for {grant.resource}")

        cancelled: list[str] = []
        if grant.generation > current_generation:
            cancelled = self.store.cancel_pending_for_resource(grant.resource, exempt_states=_SETTLED_LEDGER_STATES)

        grant_expires_at = (now + timedelta(milliseconds=grant.ttl_ms)).isoformat()
        self.store.upsert_fence_state(resource=grant.resource, generation=grant.generation, holder=grant.holder,
                                      grant_expires_at=grant_expires_at, advanced_at=now.isoformat())
        return AdvanceResult(accepted=True, bridge_epoch=self.bridge_epoch,
                             generation=grant.generation, cancelled=cancelled)

    def submit(self, *, authorization: WriteAuthorization, request_fingerprint: str,
               broker_call: Callable[[], dict[str, Any]],
               request_args: dict[str, Any] | None = None) -> SubmitResult:
        del request_args
        existing = self.store.get_ledger_entry(authorization.attempt_id)
        if existing is not None:
            # Durable, unconditional idempotency: no re-verification, no second broker call,
            # regardless of what state the existing entry is in (mission section 6/7).
            return SubmitResult(existing["attempt_id"], existing["state"], existing["broker_response"])

        _verify(self.keys, authorization.signed_payload(), authorization.sig, authorization.key_id)

        if authorization.request_fingerprint != request_fingerprint:
            raise RequestFingerprintMismatch("authorization is not bound to this exact request")

        account = authorization.resource.split(":")[-1]
        if account != self.configured_account_id:
            raise WrongAccount(f"authorization for account {account!r} rejected by bridge configured for "
                               f"{self.configured_account_id!r}")

        now = self.clock()
        now_iso = now.isoformat()
        exp = datetime.fromisoformat(authorization.exp)
        if now > exp:
            self.store.upsert_ledger_entry(attempt_id=authorization.attempt_id, resource=authorization.resource,
                                           state="EXPIRED_BEFORE_DISPATCH", broker_response=None, now=now_iso)
            return SubmitResult(authorization.attempt_id, "EXPIRED_BEFORE_DISPATCH")

        state = self.store.get_fence_state(authorization.resource)
        if state is not None and authorization.generation < state["generation"]:
            fence_ok = False
        else:
            # The production MCP bridge receives no separate advance-fence request. A valid
            # signed authorization advances the durable generation atomically at the boundary;
            # a lower generation is rejected above. The authorization expiry bounds this inline
            # grant for the single dispatch.
            if state is None or authorization.generation > state["generation"]:
                self.store.upsert_fence_state(resource=authorization.resource,
                                               generation=authorization.generation, holder="signed-platform",
                                               grant_expires_at=authorization.exp, advanced_at=now_iso)
                state = self.store.get_fence_state(authorization.resource)
            fence_ok = (state is not None and state["generation"] == authorization.generation
                       and state["grant_expires_at"] is not None
                       and now <= datetime.fromisoformat(state["grant_expires_at"]))
        if not fence_ok:
            self.store.upsert_ledger_entry(attempt_id=authorization.attempt_id, resource=authorization.resource,
                                           state="CANCELLED_FENCED", broker_response=None, now=now_iso)
            return SubmitResult(authorization.attempt_id, "CANCELLED_FENCED")

        # Durable write BEFORE the broker call: a crash after this point but before the call
        # returns (or before the DISPATCHED write below) leaves this exact row on disk, and
        # restart()'s sweep converts it to UNCERTAIN_AFTER_RESTART - never silently resolved.
        self.store.upsert_ledger_entry(attempt_id=authorization.attempt_id, resource=authorization.resource,
                                       state="SUBMISSION_IN_PROGRESS", broker_response=None, now=now_iso)

        broker_response = broker_call()

        self.store.upsert_ledger_entry(attempt_id=authorization.attempt_id, resource=authorization.resource,
                                       state="DISPATCHED", broker_response=broker_response,
                                       now=self.clock().isoformat())
        return SubmitResult(authorization.attempt_id, "DISPATCHED", broker_response)

    def ledger_entry(self, attempt_id: str) -> SubmitResult | None:
        entry = self.store.get_ledger_entry(attempt_id)
        if entry is None:
            return None
        return SubmitResult(entry["attempt_id"], entry["state"], entry["broker_response"])
