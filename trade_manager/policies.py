"""Versioned strategy-specific management policy resolution."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ManagementPolicyConfig:
    policy_id: str
    version: str
    strategy_id: str
    experimental: bool = True
    enabled: bool = True
    breakeven: dict[str, Any] = field(default_factory=dict)
    profit_protection: dict[str, Any] = field(default_factory=dict)
    trailing: dict[str, Any] = field(default_factory=dict)
    partial_reduction: dict[str, Any] = field(default_factory=dict)
    early_exit: dict[str, Any] = field(default_factory=dict)
    adding: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "portfolio_authorization_required": True})
    reentry: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "portfolio_authorization_required": True})
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f"{self.policy_id}:{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, "version": self.version, "strategy_id": self.strategy_id,
                "experimental": self.experimental, "enabled": self.enabled, "breakeven": self.breakeven,
                "profit_protection": self.profit_protection, "trailing": self.trailing,
                "partial_reduction": self.partial_reduction, "early_exit": self.early_exit,
                "adding": self.adding, "reentry": self.reentry, "context": self.context}


NO_MANAGEMENT_POLICY = "NO_MANAGEMENT_POLICY"


def context_v1_experiment() -> ManagementPolicyConfig:
    return ManagementPolicyConfig(
        policy_id="CONTEXT_V1_MANAGEMENT_EXPERIMENT_V1", version="1",
        strategy_id="CONTEXT_STRUCTURE_RETRACE_V1", experimental=True,
        breakeven={"enabled": False, "activation_r": None, "protected_r": 0.0, "parameter_status": "UNVALIDATED"},
        profit_protection={"enabled": False, "activation": None, "minimum_locked_profit_r": None, "parameter_status": "UNVALIDATED"},
        trailing={"enabled": False, "method": None, "activation_r": None, "minimum_improvement": None, "cooldown_seconds": None, "parameter_status": "UNVALIDATED"},
        partial_reduction={"enabled": False, "conditions": [], "reduction_percent": None, "parameter_status": "UNVALIDATED"},
        early_exit={"enabled": False, "conditions": [], "parameter_status": "UNVALIDATED"},
        adding={"enabled": False, "conditions": [], "maximum_additions": 0, "portfolio_authorization_required": True},
        reentry={"enabled": False, "conditions": [], "maximum_reentries": 0, "portfolio_authorization_required": True},
        context={"levels": ["EMA200", "STRUCTURE", "ATR"], "strategy_provided_levels": True},
    )


class PolicyRegistry:
    def __init__(self) -> None:
        self._strategy_defaults: dict[str, ManagementPolicyConfig] = {}
        self._overrides: dict[tuple[str, str], ManagementPolicyConfig] = {}

    def register_strategy_default(self, policy: ManagementPolicyConfig) -> None:
        self._strategy_defaults[policy.strategy_id] = policy

    def register_instrument_override(self, strategy_id: str, symbol: str, policy: ManagementPolicyConfig) -> None:
        base = self._strategy_defaults.get(strategy_id)
        if base is None:
            self._overrides[(strategy_id, symbol)] = policy
            return
        # Overrides inherit strategy defaults; only non-empty override sections replace them.
        sections = {}
        for name in ("breakeven", "profit_protection", "trailing", "partial_reduction", "early_exit", "adding", "reentry", "context"):
            override_value = getattr(policy, name)
            sections[name] = {**getattr(base, name), **override_value} if isinstance(override_value, dict) else override_value
        self._overrides[(strategy_id, symbol)] = replace(base, policy_id=policy.policy_id, version=policy.version,
                                                         experimental=policy.experimental, enabled=policy.enabled, **sections)

    def resolve(self, strategy_id: str, symbol: str) -> ManagementPolicyConfig | None:
        return self._overrides.get((strategy_id, symbol)) or self._strategy_defaults.get(strategy_id)

    def resolution(self, position: dict[str, Any]) -> dict[str, Any]:
        policy = self.resolve(str(position.get("strategy_id", "")), str(position.get("symbol", "")))
        return {"strategy_id": position.get("strategy_id"), "symbol": position.get("symbol"),
                "policy": policy, "management_policy_id": policy.policy_id if policy else NO_MANAGEMENT_POLICY,
                "management_policy_version": policy.version if policy else None,
                "resolution": "INSTRUMENT_OVERRIDE" if policy and (str(position.get("strategy_id", "")), str(position.get("symbol", ""))) in self._overrides else ("STRATEGY_DEFAULT" if policy else "NONE")}


def default_registry() -> PolicyRegistry:
    registry = PolicyRegistry()
    registry.register_strategy_default(context_v1_experiment())
    return registry
