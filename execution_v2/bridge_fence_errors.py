"""Shared exception hierarchy for every independent bridge-side rejection - both the test-only
in-memory simulator (`bridge_fence_sim.py`) and the real, durable bridge boundary
(`mt5_bridge_fence/boundary.py`) raise these SAME classes. A caller (worker.py, a test) that
catches `WrongAccount` catches it regardless of which boundary implementation is behind it -
audit-critical, since mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION` section 2
requires production code to be provably incapable of reaching the simulator; keeping the
exception types identical means swapping the simulator for the real boundary (as this
remediation does) changes zero calling code in `execution_v2/worker.py`.
"""
from __future__ import annotations


class BridgeFenceError(RuntimeError):
    """Base for every independent rejection a bridge fence boundary can raise."""


class InvalidSignature(BridgeFenceError):
    pass


class ExpiredGrant(BridgeFenceError):
    pass


class ExpiredAuthorization(BridgeFenceError):
    pass


class StaleGeneration(BridgeFenceError):
    pass


class WrongAccount(BridgeFenceError):
    pass


class RequestFingerprintMismatch(BridgeFenceError):
    pass
