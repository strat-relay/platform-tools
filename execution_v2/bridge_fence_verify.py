"""The one shared "verify this signed payload or raise" function every independent bridge fence
boundary calls - the test-only simulator (`bridge_fence_sim.py`) and the real, durable boundary
(`mt5_bridge_fence/boundary.py`) both import THIS, rather than each defining their own wrapper
around `fence.verify`, so the two can never drift on what counts as a valid/invalid signature or
an unknown key id.
"""
from __future__ import annotations

from typing import Any

from .bridge_fence_errors import InvalidSignature
from .fence import verify as _verify_signature


def verify_or_raise(keys: dict[str, bytes], payload: dict[str, Any], sig: str, key_id: str) -> None:
    if key_id not in keys:
        raise InvalidSignature(f"unknown key_id: {key_id}")
    if not _verify_signature(keys[key_id], payload, sig):
        raise InvalidSignature("signature verification failed")
