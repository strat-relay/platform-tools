"""Deterministic identity for every V2 execution object. Same construction already used
throughout this codebase (orchestration/models.py, trade_manager/central.py, migration/signal.py,
trade_management/ids.py) - defined locally per the established per-domain convention.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def execution_intent_id(*, entry_signal_id: str, account_id: str) -> str:
    """One intent per (entry_signal_id, account_id): deterministic and idempotent - the same
    canonical EntrySignal, consumed any number of times for the same personal account, always
    yields the same execution_intent_id."""
    return stable_id("EXECV2", {"entry_signal_id": entry_signal_id, "account_id": account_id})


def attempt_id(*, execution_intent_id: str) -> str:
    """One attempt per intent for this first V2 slice (see migration 016's own note); the
    attempt_id IS the bridge idempotency key (docs/runtime_boundaries/04 section 2.3: "Keyed by
    attempt_id (== idempotency_key)")."""
    return stable_id("ATT", {"execution_intent_id": execution_intent_id})


def execution_result_id(*, attempt_id: str) -> str:
    return stable_id("EXECRES", {"attempt_id": attempt_id})


def finding_id(*, attempt_id: str, queried_at: str) -> str:
    return stable_id("RECON", {"attempt_id": attempt_id, "queried_at": queried_at})
