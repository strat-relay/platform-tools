"""Deliberately small, explicit personal-execution risk boundary (mission section 4). Not a
risk engine: a fail-closed gate over a small, versioned, human-reviewed JSON configuration.

Ships with `enabled: false` by default (`orchestration/config/v2_execution_risk_policy.json`) -
every signal is blocked until an operator explicitly supplies and approves a live configuration.
No financially meaningful value (volume cap, allowed account) is hard-coded here or defaulted to
something "reasonable" merely to make the path runnable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RISK_POLICY_PATH = ROOT / "orchestration" / "config" / "v2_execution_risk_policy.json"


class RiskPolicyError(ValueError):
    """The configuration cannot be safely resolved; callers must treat this as BLOCKED, never
    as "fall back to a default value"."""


@dataclass(frozen=True)
class RiskPolicy:
    version: int
    enabled: bool
    max_volume: float
    allowed_symbols: tuple[str, ...] | None  # None = not restricted by symbol; () = none allowed
    allowed_accounts: tuple[str, ...]
    max_signal_age_seconds: float
    source: str


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskPolicyError(f"{name} must be numeric")
    result = float(value)
    if result <= 0:
        raise RiskPolicyError(f"{name} must be greater than zero")
    return result


def load_risk_policy(path: Path | str = DEFAULT_RISK_POLICY_PATH) -> RiskPolicy:
    policy_path = Path(path)
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiskPolicyError(f"unable to load risk policy: {policy_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RiskPolicyError("risk policy must be a JSON object")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise RiskPolicyError("version must be a positive integer")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise RiskPolicyError("enabled must be an explicit boolean")
    if not enabled:
        # Fail closed without validating the rest: a blocked policy needs no other field to be
        # "correct" yet - this is exactly the "non-executable/blocked configuration" the mission
        # requires when no approved live sizing configuration exists.
        return RiskPolicy(version=version, enabled=False, max_volume=0.0, allowed_symbols=(),
                          allowed_accounts=(), max_signal_age_seconds=0.0, source=str(policy_path))
    max_volume = _finite_positive(raw.get("max_volume"), "max_volume")
    max_age = _finite_positive(raw.get("max_signal_age_seconds"), "max_signal_age_seconds")
    accounts = raw.get("allowed_accounts")
    if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a.strip() for a in accounts):
        raise RiskPolicyError("allowed_accounts must be a non-empty list of account id strings")
    symbols = raw.get("allowed_symbols")
    if symbols is not None and (not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols)):
        raise RiskPolicyError("allowed_symbols, if present, must be a list of strings")
    return RiskPolicy(version=version, enabled=True, max_volume=max_volume,
                      allowed_symbols=tuple(symbols) if symbols is not None else None,
                      allowed_accounts=tuple(accounts), max_signal_age_seconds=max_age,
                      source=str(policy_path))
