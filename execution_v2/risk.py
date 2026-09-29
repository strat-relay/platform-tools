"""Explicit, fail-closed V2 execution risk policy.

Ships with `enabled: false` by default (`orchestration/config/v2_execution_risk_policy.json`) -
every signal is blocked until an operator explicitly supplies and approves a live configuration.
No financially meaningful value (volume cap, allowed account) is hard-coded here or defaulted to
something "reasonable" merely to make the path runnable.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RISK_POLICY_PATH = ROOT / "orchestration" / "config" / "v2_execution_risk_policy.json"


class RiskPolicyError(ValueError):
    """The configuration cannot be safely resolved; callers must treat this as BLOCKED, never
    as "fall back to a default value"."""


class RiskPolicyRevisionConflict(RiskPolicyError):
    """The submitted policy revision is older than the canonical PostgreSQL revision."""


@dataclass(frozen=True)
class RiskPolicy:
    version: int
    enabled: bool
    max_volume: float
    allowed_symbols: tuple[str, ...] | None  # None = not restricted by symbol; () = none allowed
    allowed_accounts: tuple[str, ...]
    max_signal_age_seconds: float
    source: str
    allowed_strategies: tuple[str, ...] = ()
    risk_per_trade: float = 0.0
    max_daily_loss: float = 0.0
    max_concurrent_positions: int = 0
    max_concurrent_orders: int = 0
    max_account_exposure: float = 0.0
    duplicate_position_policy: str = "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY"
    canary_max_new_executions: int = 0


@dataclass(frozen=True)
class RiskDecision:
    permitted: bool
    reason: str | None = None
    volume: float | None = None
    risk_amount: float | None = None


_REQUIRED_BOUNDS = (
    "risk_per_trade", "max_volume", "max_signal_age_seconds", "max_daily_loss",
    "max_concurrent_positions", "max_concurrent_orders", "max_account_exposure",
    "canary_max_new_executions",
)


def _finite_non_negative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskPolicyError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise RiskPolicyError(f"{name} must be finite and non-negative")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RiskPolicyError(f"{name} must be a positive integer")
    return value


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
    return parse_policy_dict(raw, source=str(policy_path))


def parse_policy_dict(raw: Any, *, source: str) -> RiskPolicy:
    """The exact validation `load_risk_policy` has always applied, extracted so a second caller
    (mission CLAUDE-V2-RISK-EXECUTION-CONSOLE's `risk_policy_store.py`, validating an
    operator-submitted policy before it is ever persisted) can reuse it byte-for-byte rather than
    re-implementing or drifting from it. `load_risk_policy` above is unchanged in behavior - it
    only now delegates here instead of inlining this logic."""
    if not isinstance(raw, dict):
        raise RiskPolicyError("risk policy must be a JSON object")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise RiskPolicyError("version must be a positive integer")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise RiskPolicyError("enabled must be an explicit boolean")
    if not enabled:
        # Preserve explicitly reviewed disabled-policy values for observability, but never make
        # them executable: the evaluator still returns RISK_POLICY_DISABLED before using them.
        accounts = raw.get("allowed_accounts") if isinstance(raw.get("allowed_accounts"), list) else []
        symbols = raw.get("allowed_symbols") if isinstance(raw.get("allowed_symbols"), list) else []
        strategies = raw.get("allowed_strategies") if isinstance(raw.get("allowed_strategies"), list) else []
        def disabled_number(name: str, default: float = 0.0) -> float:
            value = raw.get(name)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else default
        def disabled_int(name: str) -> int:
            value = raw.get(name)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        return RiskPolicy(version=version, enabled=False, max_volume=disabled_number("max_volume"),
                          allowed_symbols=tuple(str(s) for s in symbols if isinstance(s, str)),
                          allowed_accounts=tuple(str(a) for a in accounts if isinstance(a, str)),
                          max_signal_age_seconds=disabled_number("max_signal_age_seconds"),
                          source=source, allowed_strategies=tuple(str(s) for s in strategies if isinstance(s, str)),
                          risk_per_trade=disabled_number("risk_per_trade"),
                          max_daily_loss=disabled_number("max_daily_loss"),
                          max_concurrent_positions=disabled_int("max_concurrent_positions"),
                          max_concurrent_orders=disabled_int("max_concurrent_orders"),
                          max_account_exposure=disabled_number("max_account_exposure"),
                          duplicate_position_policy=str(raw.get("duplicate_position_policy") or "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY"),
                          canary_max_new_executions=disabled_int("canary_max_new_executions"))
    max_volume = _finite_positive(raw.get("max_volume"), "max_volume")
    max_age = _finite_positive(raw.get("max_signal_age_seconds"), "max_signal_age_seconds")
    accounts = raw.get("allowed_accounts")
    if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a.strip() for a in accounts):
        raise RiskPolicyError("allowed_accounts must be a non-empty list of account id strings")
    symbols = raw.get("allowed_symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) and s.strip() for s in symbols):
        raise RiskPolicyError("allowed_symbols must be a non-empty list of canonical symbol strings")
    strategies = raw.get("allowed_strategies")
    if not isinstance(strategies, list) or not strategies or not all(isinstance(s, str) and s.strip() for s in strategies):
        raise RiskPolicyError("allowed_strategies must be a non-empty list of stable strategy refs")
    risk_per_trade = _finite_positive(raw.get("risk_per_trade"), "risk_per_trade")
    daily_loss = _finite_positive(raw.get("max_daily_loss"), "max_daily_loss")
    exposure = _finite_positive(raw.get("max_account_exposure"), "max_account_exposure")
    max_positions = _positive_int(raw.get("max_concurrent_positions"), "max_concurrent_positions")
    max_orders = _positive_int(raw.get("max_concurrent_orders"), "max_concurrent_orders")
    canary = _positive_int(raw.get("canary_max_new_executions"), "canary_max_new_executions")
    duplicate = raw.get("duplicate_position_policy")
    if duplicate not in {"REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY", "REJECT_SAME_ACCOUNT_SYMBOL"}:
        raise RiskPolicyError("duplicate_position_policy is invalid")
    return RiskPolicy(version=version, enabled=True, max_volume=max_volume,
                      allowed_symbols=tuple(symbols), allowed_accounts=tuple(accounts),
                      max_signal_age_seconds=max_age, source=source,
                      allowed_strategies=tuple(strategies), risk_per_trade=risk_per_trade,
                      max_daily_loss=daily_loss, max_concurrent_positions=max_positions,
                      max_concurrent_orders=max_orders, max_account_exposure=exposure,
                      duplicate_position_policy=duplicate, canary_max_new_executions=canary)


def evaluate_candidate(record: Mapping[str, Any], *, policy: RiskPolicy, account_id: str,
                       now_utc: Any, broker: Mapping[str, Any], account: Mapping[str, Any],
                       state: Mapping[str, Any]) -> RiskDecision:
    """Evaluate a candidate without I/O. Missing safety state is always a rejection."""
    if not policy.enabled:
        return RiskDecision(False, "RISK_POLICY_DISABLED")
    if account_id not in policy.allowed_accounts:
        return RiskDecision(False, "ACCOUNT_NOT_ALLOWED")
    strategy_ref = record.get("strategy_ref") or f"{record.get('strategy_id')}@{record.get('strategy_version')}"
    if strategy_ref not in policy.allowed_strategies:
        return RiskDecision(False, "STRATEGY_NOT_ALLOWED")
    symbol = record.get("instrument")
    if symbol not in (policy.allowed_symbols or ()):
        return RiskDecision(False, "SYMBOL_NOT_ALLOWED")
    # Canary counters are historical/observability data only.  Normal V2 execution is
    # guarded by the explicit authority switch and the safety limits below; it must not
    # become unavailable merely because an old canary window is exhausted or absent.
    required_state = ("daily_loss", "concurrent_positions", "concurrent_orders", "account_exposure")
    if any(k not in state or state[k] is None for k in required_state):
        return RiskDecision(False, "RISK_STATE_UNAVAILABLE")
    if float(state["daily_loss"]) >= policy.max_daily_loss:
        return RiskDecision(False, "DAILY_LOSS_LIMIT_EXCEEDED")
    if int(state["concurrent_positions"]) >= policy.max_concurrent_positions:
        return RiskDecision(False, "MAX_CONCURRENT_POSITIONS_EXCEEDED")
    if int(state["concurrent_orders"]) >= policy.max_concurrent_orders:
        return RiskDecision(False, "MAX_CONCURRENT_ORDERS_EXCEEDED")
    if float(state["account_exposure"]) >= policy.max_account_exposure:
        return RiskDecision(False, "MAX_ACCOUNT_EXPOSURE_EXCEEDED")
    entry, stop = record.get("entry_price"), record.get("stop_price")
    if entry is None or stop is None or float(entry) <= 0 or float(stop) <= 0:
        return RiskDecision(False, "INVALID_GEOMETRY")
    signal_emitted_at = record.get("signal_emitted_at")
    if signal_emitted_at is None:
        return RiskDecision(False, "MISSING_SIGNAL_EMITTED_AT")
    emitted = signal_emitted_at if isinstance(signal_emitted_at, datetime) else datetime.fromisoformat(str(signal_emitted_at).replace("Z", "+00:00"))
    if emitted.tzinfo is None:
        emitted = emitted.replace(tzinfo=timezone.utc)
    age = (now_utc - emitted).total_seconds()
    if age > policy.max_signal_age_seconds:
        return RiskDecision(False, "STALE_SIGNAL")
    if "equity" not in account or any(key not in broker for key in ("tick_size", "tick_value", "volume_min", "volume_max", "volume_step")):
        return RiskDecision(False, "BROKER_METADATA_UNAVAILABLE")
    # The risk budget is a fraction of the sizing capital: equity, or free margin when the context
    # sizes from free margin (account["sizing_capital"]).
    capital = float(account["sizing_capital"] if account.get("sizing_capital") is not None else account.get("equity"))
    tick_size, tick_value = float(broker["tick_size"]), float(broker["tick_value"])
    minimum, maximum, step = map(float, (broker["volume_min"], broker["volume_max"], broker["volume_step"]))
    risk_budget = capital * policy.risk_per_trade
    per_lot_risk = abs(float(entry) - float(stop)) / tick_size * tick_value
    raw = risk_budget / per_lot_risk if per_lot_risk > 0 else 0.0
    if raw + 1e-9 < minimum:
        return RiskDecision(False, "MINIMUM_LOT_EXCEEDS_RISK_LIMIT")
    # Add only a representational epsilon before flooring; the post-normalization risk check
    # remains authoritative and prevents rounding upward past the monetary ceiling.
    volume = math.floor((min(raw, policy.max_volume, maximum) / step) + 1e-9) * step
    if volume < minimum or volume <= 0:
        return RiskDecision(False, "VOLUME_BELOW_BROKER_MINIMUM")
    risk_amount = volume * per_lot_risk
    if risk_amount > risk_budget + 1e-9:
        return RiskDecision(False, "NORMALIZED_VOLUME_EXCEEDS_RISK_LIMIT")
    return RiskDecision(True, volume=volume, risk_amount=risk_amount)
