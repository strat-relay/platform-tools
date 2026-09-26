"""Shared result types and the structural interface for every bridge fence boundary
implementation - the test-only simulator (`bridge_fence_sim.py`), the real durable boundary
(`mt5_bridge_fence/boundary.py`), and the real HTTP client
(`execution_v2/runtime/bridge_client.py::HttpBridgeFenceClient`) all return these SAME frozen
dataclasses and satisfy the SAME `BridgeFence` Protocol, so `execution_v2/worker.py` and
`execution_v2/reconcile.py` treat every implementation identically and neither needs to import
anything from the test-only simulator module (verified by
`tests/test_execution_v2_isolation.py::test_no_production_module_imports_the_test_only_simulator`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class SubmitResult:
    attempt_id: str
    state: str  # DISPATCHED | CANCELLED_FENCED | EXPIRED_BEFORE_DISPATCH | UNCERTAIN_AFTER_RESTART | SUBMISSION_IN_PROGRESS
    broker_response: dict[str, Any] | None = None


@dataclass
class AdvanceResult:
    accepted: bool
    bridge_epoch: int
    generation: int
    cancelled: list[str] = field(default_factory=list)


class BridgeFence(Protocol):
    """Structural interface every bridge fence boundary implementation satisfies. `worker.py`
    and `reconcile.py` depend on THIS, never a concrete class, so swapping the implementation
    they are constructed with (as this remediation does - simulator -> HttpBridgeFenceClient in
    production) changes zero calling code."""

    def advance_fence(self, grant: Any) -> AdvanceResult: ...

    def submit(self, *, authorization: Any, request_fingerprint: str,
              broker_call: Callable[[], dict[str, Any]],
              request_args: dict[str, Any] | None = None) -> SubmitResult: ...

    def ledger_entry(self, attempt_id: str) -> SubmitResult | None: ...
