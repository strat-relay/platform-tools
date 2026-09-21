"""Centralized, fail-closed REAL execution risk-policy resolution."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RISK_POLICY_PATH = ROOT / "orchestration" / "config" / "risk_policy.json"


class RiskPolicyError(ValueError):
    """Raised when REAL sizing cannot safely resolve a risk policy."""


@dataclass(frozen=True)
class ResolvedRiskPolicy:
    version: int
    virtual_equity_usd: float
    risk_percent: float
    risk_fraction: float
    risk_budget_usd: float
    source: str


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskPolicyError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RiskPolicyError(f"{name} must be finite")
    if positive and result <= 0:
        raise RiskPolicyError(f"{name} must be greater than zero")
    return result


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RiskPolicyError("risk policy must be an object")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise RiskPolicyError("version must be a positive integer")
    portfolio, defaults, strategies = raw.get("portfolio"), raw.get("defaults"), raw.get("strategies")
    if not isinstance(portfolio, dict) or not isinstance(defaults, dict) or not isinstance(strategies, dict):
        raise RiskPolicyError("portfolio, defaults, and strategies are required objects")
    _finite_number(portfolio.get("virtual_equity_usd"), "portfolio.virtual_equity_usd", positive=True)
    default_percent = _finite_number(defaults.get("risk_percent_per_position"), "defaults.risk_percent_per_position")
    if not 0 < default_percent <= 100:
        raise RiskPolicyError("defaults.risk_percent_per_position must be in (0, 100]")
    for strategy_id, override in strategies.items():
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise RiskPolicyError("strategy IDs must be non-empty strings")
        if not isinstance(override, dict):
            raise RiskPolicyError(f"strategy override must be an object: {strategy_id}")
        percent = _finite_number(override.get("risk_percent_per_position"), f"strategies.{strategy_id}.risk_percent_per_position")
        if not 0 < percent <= 100:
            raise RiskPolicyError(f"strategy risk percent must be in (0, 100]: {strategy_id}")
    return raw


def load_risk_policy(path: Path | str = RISK_POLICY_PATH) -> dict[str, Any]:
    policy_path = Path(path)
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiskPolicyError(f"unable to load risk policy: {policy_path}: {exc}") from exc
    return _validate(raw)


class RiskPolicyResolver:
    """Mtime-aware loader; edits apply to the next NEW sizing decision."""

    def __init__(self, path: Path | str = RISK_POLICY_PATH):
        self.path = Path(path)
        self._signature: tuple[int, int] | None = None
        self._policy: dict[str, Any] | None = None

    def _load_if_changed(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            raise RiskPolicyError(f"unable to stat risk policy: {self.path}: {exc}") from exc
        if self._policy is None or signature != self._signature:
            self._policy = load_risk_policy(self.path)
            self._signature = signature
        return self._policy

    def for_strategy(self, strategy_id: str) -> ResolvedRiskPolicy:
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise RiskPolicyError("strategy_id must be a non-empty string")
        policy = self._load_if_changed()
        override = policy["strategies"].get(strategy_id)
        if override is not None:
            percent, source = float(override["risk_percent_per_position"]), "STRATEGY_OVERRIDE"
        elif "risk_percent_per_position" in policy["defaults"]:
            percent, source = float(policy["defaults"]["risk_percent_per_position"]), "DEFAULT"
        else:
            raise RiskPolicyError(f"no risk policy for strategy: {strategy_id}")
        equity = float(policy["portfolio"]["virtual_equity_usd"])
        return ResolvedRiskPolicy(int(policy["version"]), equity, percent, percent / 100.0,
                                  equity * percent / 100.0, source)


_DEFAULT_RESOLVER = RiskPolicyResolver()


def risk_policy_for(strategy_id: str) -> ResolvedRiskPolicy:
    return _DEFAULT_RESOLVER.for_strategy(strategy_id)
