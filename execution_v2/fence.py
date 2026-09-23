"""OD-06 fence authority (platform side): mints signed, expiring FenceGrant and
WriteAuthorization objects (docs/runtime_boundaries/15_ADR_OD06_BROKER_WRITE_FENCING.md,
04_RECOMMENDED_FENCING_DESIGN.md section 2). HMAC-SHA256 (V1 shared-secret model, ADR section 9
risk 4 - asymmetric signatures are a documented later hardening step, not required for this
first slice).

PostgreSQL (`platform.ownership_leases` / `platform.acquire_ownership`, unmodified) remains the
sole source of truth for *who* owns a resource and at *which generation*. This module only signs
what PostgreSQL has already decided - it never decides ownership itself. `bridge_fence_sim.py`
is the independent verifier: it re-checks the signature, expiry and generation itself rather
than trusting the caller, exactly as the ADR requires ("the bridge must independently reject").
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes

DEFAULT_GRANT_TTL_MS = 20_000  # ADR 04 section 7
DEFAULT_AUTHORIZATION_TTL_S = 5.0  # matches X-Bridge-Max-Age-Ms and intent_max_age (ADR 04 section 7)


class FenceAuthorityError(RuntimeError):
    """Fail closed: no grant or authorization can be minted."""


@dataclass(frozen=True)
class FenceGrant:
    resource: str
    generation: int
    holder: str
    ttl_ms: int
    not_after: str
    key_id: str
    sig: str

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource, "generation": self.generation, "holder": self.holder,
                "ttl_ms": self.ttl_ms, "not_after": self.not_after, "key_id": self.key_id, "sig": self.sig}

    def signed_payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.to_dict().items() if k != "sig"}


@dataclass(frozen=True)
class WriteAuthorization:
    resource: str
    generation: int
    attempt_id: str
    tool: str
    request_fingerprint: str
    scope_class: str  # EXPOSURE_INCREASING | REDUCE_ONLY
    exp: str
    key_id: str
    sig: str

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource, "generation": self.generation, "attempt_id": self.attempt_id,
                "tool": self.tool, "request_fingerprint": self.request_fingerprint,
                "scope_class": self.scope_class, "exp": self.exp, "key_id": self.key_id, "sig": self.sig}

    def signed_payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.to_dict().items() if k != "sig"}


def _sign(key: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(key, canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify(key: bytes, payload: dict[str, Any], sig: str) -> bool:
    """The single, shared, constant-time signature check. Both the test-only simulator
    (`bridge_fence_sim.py`) and the real bridge boundary (`mt5_bridge_fence/boundary.py`) call
    this exact function - never their own reimplementation - so the two verifiers can never
    silently drift apart on what counts as a valid signature."""
    expected = _sign(key, payload)
    return hmac.compare_digest(expected, sig)


class FenceAuthority:
    """`keys`: {key_id: secret_bytes}. Exactly one key is `active_key_id` (used for new
    signatures); older keys may remain present only to let in-flight grants/authorizations
    verify during a rotation - this module never verifies (that is the bridge's job), so old
    keys are not even consulted here, only kept for symmetry with a future verifying caller."""

    def __init__(self, *, keys: dict[str, bytes], active_key_id: str) -> None:
        if not keys:
            raise FenceAuthorityError("no fence signing keys configured")
        if active_key_id not in keys:
            raise FenceAuthorityError(f"active_key_id {active_key_id!r} not present in keys")
        if any(len(k) < 32 for k in keys.values()):
            raise FenceAuthorityError("fence signing keys must be at least 32 bytes")
        self.keys = dict(keys)
        self.active_key_id = active_key_id

    @classmethod
    def from_env(cls) -> "FenceAuthority":
        """Fails closed: refuses to start with a missing, empty, or placeholder key. No
        default/example key is ever accepted - see execution_v2/runtime/config.py."""
        raw = os.getenv("V2_FENCE_SIGNING_KEY")
        key_id = os.getenv("V2_FENCE_KEY_ID", "v2-fence-key-1")
        if not raw or not raw.strip():
            raise FenceAuthorityError("V2_FENCE_SIGNING_KEY is required")
        key_bytes = raw.strip().encode("utf-8")
        if len(key_bytes) < 32:
            raise FenceAuthorityError("V2_FENCE_SIGNING_KEY must be at least 32 bytes")
        return cls(keys={key_id: key_bytes}, active_key_id=key_id)

    def mint_grant(self, *, resource: str, generation: int, holder: str,
                   ttl_ms: int = DEFAULT_GRANT_TTL_MS, now: datetime | None = None) -> FenceGrant:
        now = now or datetime.now(timezone.utc)
        not_after = (now + timedelta(milliseconds=ttl_ms)).isoformat(timespec="milliseconds")
        payload = {"resource": resource, "generation": generation, "holder": holder,
                  "ttl_ms": ttl_ms, "not_after": not_after, "key_id": self.active_key_id}
        sig = _sign(self.keys[self.active_key_id], payload)
        return FenceGrant(**payload, sig=sig)

    def mint_authorization(self, *, resource: str, generation: int, attempt_id: str, tool: str,
                           request_fingerprint: str, scope_class: str = "EXPOSURE_INCREASING",
                           ttl_s: float = DEFAULT_AUTHORIZATION_TTL_S,
                           now: datetime | None = None) -> WriteAuthorization:
        if scope_class not in ("EXPOSURE_INCREASING", "REDUCE_ONLY"):
            raise FenceAuthorityError(f"invalid scope_class: {scope_class}")
        now = now or datetime.now(timezone.utc)
        exp = (now + timedelta(seconds=ttl_s)).isoformat(timespec="milliseconds")
        payload = {"resource": resource, "generation": generation, "attempt_id": attempt_id, "tool": tool,
                  "request_fingerprint": request_fingerprint, "scope_class": scope_class,
                  "exp": exp, "key_id": self.active_key_id}
        sig = _sign(self.keys[self.active_key_id], payload)
        return WriteAuthorization(**payload, sig=sig)
