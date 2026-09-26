"""TEST-ONLY in-process simulation of the bridge-side half of OD-06
(docs/runtime_boundaries/15_ADR_OD06_BROKER_WRITE_FENCING.md,
04_RECOMMENDED_FENCING_DESIGN.md section 3). This is NOT the real mt5-native-bridge process and
is NOT the real bridge-side fence implementation either - that is `mt5_bridge_fence/boundary.py`
(`RealBridgeFenceBoundary`), added by mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION`
to remediate the prior audit finding that this module was wired into production configuration.

**This module must never be importable from production runtime wiring.** Proven by
`tests/test_execution_v2_isolation.py::test_no_production_module_imports_the_test_only_simulator`
(an AST import-graph check over `execution_v2/runtime/*.py` and `mt5_bridge_fence/*.py`) and by
`execution_v2/runtime/config.py`/`service.py` never importing this module at all - not even
behind a flag. It exists purely so `tests/test_execution_v2_fence.py`'s fast, dependency-free
in-memory proof of the OD-06 *protocol* (independent of any storage engine) keeps working; the
in-memory, per-process, non-durable storage here would fail every durability requirement
(section 5/6 of the remediation mission) a real bridge boundary must meet.

The defining property both this module and the real boundary share: **never trust a caller's
claim that a check already passed.** Every `advance_fence`/`submit` call re-verifies the
signature, generation, expiry, and account binding itself, using its own copy of the key material
and its own clock (`fence.verify`, the exact function `mt5_bridge_fence/boundary.py` also calls).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .bridge_fence_errors import (BridgeFenceError, ExpiredAuthorization, ExpiredGrant,
                                  InvalidSignature, RequestFingerprintMismatch, StaleGeneration,
                                  WrongAccount)
from .bridge_fence_types import AdvanceResult, SubmitResult
from .bridge_fence_verify import verify_or_raise as _verify
from .fence import FenceGrant, WriteAuthorization

__all__ = ["BridgeFenceError", "InvalidSignature", "ExpiredGrant", "ExpiredAuthorization",
          "StaleGeneration", "WrongAccount", "RequestFingerprintMismatch",
          "SubmitResult", "AdvanceResult", "BridgeFenceSimulator"]


@dataclass
class _ResourceFenceState:
    generation: int = 0
    grant_expires_at: datetime | None = None
    holder: str | None = None
    advanced_at: datetime | None = None


class BridgeFenceSimulator:
    """`keys` must be the SAME key material the fence authority signs with, standing in for the
    bridge's own out-of-band-provisioned copy (in the real bridge this would never traverse the
    platform's own request path). `configured_account_id` models which single account this
    simulated bridge instance is wired to serve - an authorization whose resource names a
    different account is independently rejected (`WrongAccount`), regardless of an otherwise
    valid signature/generation/expiry."""

    def __init__(self, *, keys: dict[str, bytes], configured_account_id: str,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.keys = keys
        self.configured_account_id = configured_account_id
        self.clock = clock
        self.bridge_epoch = 0
        self._fence_state: dict[str, _ResourceFenceState] = {}
        self._ledger: dict[str, SubmitResult] = {}

    def restart(self) -> None:
        """Fence state is modelled as persisted (fsync'd before ack, per the ADR) and survives;
        `bridge_epoch` increments so callers detect the restart. Any in-flight DISPATCHED ledger
        entry becomes UNCERTAIN_AFTER_RESTART - never silently resolved to success."""
        self.bridge_epoch += 1
        for attempt_id, result in list(self._ledger.items()):
            if result.state == "DISPATCHED":
                self._ledger[attempt_id] = SubmitResult(attempt_id, "UNCERTAIN_AFTER_RESTART", result.broker_response)

    def advance_fence(self, grant: FenceGrant) -> AdvanceResult:
        _verify(self.keys, grant.signed_payload(), grant.sig, grant.key_id)
        now = self.clock()
        not_after = datetime.fromisoformat(grant.not_after)
        if now > not_after:
            raise ExpiredGrant(f"grant for {grant.resource} expired at {grant.not_after} (now={now.isoformat()})")
        state = self._fence_state.setdefault(grant.resource, _ResourceFenceState())
        if grant.generation < state.generation:
            raise StaleGeneration(f"grant generation {grant.generation} < current {state.generation} for {grant.resource}")
        cancelled: list[str] = []
        if grant.generation > state.generation:
            for attempt_id, result in self._ledger.items():
                if result.state not in ("DISPATCHED", "CANCELLED_FENCED", "EXPIRED_BEFORE_DISPATCH", "UNCERTAIN_AFTER_RESTART"):
                    self._ledger[attempt_id] = SubmitResult(attempt_id, "CANCELLED_FENCED")
                    cancelled.append(attempt_id)
            state.generation = grant.generation
        state.grant_expires_at = now + timedelta(milliseconds=grant.ttl_ms)
        state.holder = grant.holder
        state.advanced_at = now
        return AdvanceResult(accepted=True, bridge_epoch=self.bridge_epoch, generation=state.generation, cancelled=cancelled)

    def submit(self, *, authorization: WriteAuthorization, request_fingerprint: str,
               broker_call: Callable[[], dict[str, Any]],
               request_args: dict[str, Any] | None = None) -> SubmitResult:
        """Idempotent on `authorization.attempt_id`: a second call with the same attempt_id
        never re-verifies or re-dispatches - it returns the existing ledger entry unchanged
        (mission section 7: duplicate delivery must never produce two broker orders)."""
        existing = self._ledger.get(authorization.attempt_id)
        if existing is not None:
            return existing

        _verify(self.keys, authorization.signed_payload(), authorization.sig, authorization.key_id)

        if authorization.request_fingerprint != request_fingerprint:
            raise RequestFingerprintMismatch("authorization is not bound to this exact request")

        account = authorization.resource.split(":")[-1]
        if account != self.configured_account_id:
            raise WrongAccount(f"authorization for account {account!r} rejected by bridge configured for "
                               f"{self.configured_account_id!r}")

        now = self.clock()
        exp = datetime.fromisoformat(authorization.exp)
        if now > exp:
            result = SubmitResult(authorization.attempt_id, "EXPIRED_BEFORE_DISPATCH")
            self._ledger[authorization.attempt_id] = result
            return result

        state = self._fence_state.get(authorization.resource)
        fence_ok = (state is not None and state.generation == authorization.generation
                   and state.grant_expires_at is not None and now <= state.grant_expires_at)
        if not fence_ok:
            result = SubmitResult(authorization.attempt_id, "CANCELLED_FENCED")
            self._ledger[authorization.attempt_id] = result
            return result

        # Every independent check passed: this is the one point a broker call is ever made.
        broker_response = broker_call()
        result = SubmitResult(authorization.attempt_id, "DISPATCHED", broker_response)
        self._ledger[authorization.attempt_id] = result
        return result

    def ledger_entry(self, attempt_id: str) -> SubmitResult | None:
        return self._ledger.get(attempt_id)
