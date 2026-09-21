"""Versioned strategy-policy evaluation; still advisory/shadow only."""
from __future__ import annotations

from typing import Any

from .counterfactual import CounterfactualPosition
from .engine import ManagementPolicy, TradeManager
from .metrics import mfe_surrender
from .policies import NO_MANAGEMENT_POLICY, PolicyRegistry, default_registry
from .state import ManagementState, ManagementStateStore
from .trailing import evaluate_trailing


class Phase2TradeManager:
    def __init__(self, registry: PolicyRegistry | None = None,
                 state_store: ManagementStateStore | None = None):
        self.registry = registry or default_registry()
        self.state_store = state_store
        self.states: dict[str, ManagementState] = {}

    def evaluate(self, position: dict[str, Any], observation: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        resolution = self.registry.resolution(position)
        policy = resolution["policy"]
        market = {**observation, "current_price": observation.get("close_executable_price", observation.get("current_price")),
                  "ema_200": observation.get("market", {}).get("M5_EMA", {}).get("value"),
                  "timestamp": observation.get("timestamp", timestamp)}
        if policy is None or not policy.enabled:
            decision = TradeManager().evaluate(position, market, timestamp=timestamp)
            decision.update({"reason_codes": [NO_MANAGEMENT_POLICY], "management_policy": NO_MANAGEMENT_POLICY,
                             "management_policy_version": None, "policy_resolution": resolution["resolution"]})
            return decision
        engine_policy = ManagementPolicy(policy_id=policy.policy_id, version=policy.version,
                                         mode="ADVISORY_SHADOW",
                                         breakeven_enabled=bool(policy.breakeven.get("enabled", False)),
                                         trailing_enabled=bool(policy.trailing.get("enabled", False)),
                                         reductions_enabled=bool(policy.partial_reduction.get("enabled", False)),
                                         closes_enabled=bool(policy.early_exit.get("enabled", False)),
                                         additions_enabled=bool(policy.adding.get("enabled", False)),
                                         reentry_enabled=bool(policy.reentry.get("enabled", False)),
                                         breakeven_r=policy.breakeven.get("activation_r"),
                                         trail_distance=policy.trailing.get("minimum_improvement"),
                                         ema_tolerance=policy.context.get("ema_tolerance"))
        decision = TradeManager(engine_policy).evaluate(position, market, timestamp=timestamp)
        decision.update({"management_policy_id": policy.policy_id, "management_policy_version": policy.version,
                         "policy_resolution": resolution["resolution"], **mfe_surrender(observation.get("mfe_R"), observation.get("current_R"))})
        trailing = evaluate_trailing(position, observation, policy.trailing)
        if trailing:
            decision.update({"action": "TRAIL_STOP", "reason_codes": list(trailing.reason_codes),
                             "proposed_price": trailing.proposed_stop, "trailing_proposal": trailing.as_dict(),
                             "explanation": "Custom trailing proposal generated in advisory shadow mode."})
        if decision.get("action") in {"ADD_POSITION", "REENTRY_ELIGIBLE"}:
            decision["portfolio_authorization_required"] = True
        state = self.states.setdefault(position["economic_position_id"], ManagementState(position["economic_position_id"]))
        state.policy_id, state.policy_version = policy.policy_id, policy.version
        decision["management_state"] = state.transition(decision, timestamp)
        if self.state_store:
            self.state_store.update(state)
        return decision


def counterfactual_for(position: dict[str, Any]) -> CounterfactualPosition:
    return CounterfactualPosition(position["economic_position_id"], frozen={"status": position.get("status"),
                                  "realized_R": position.get("realized_R"), "MFE_R": position.get("mfe_R"),
                                  "MAE_R": position.get("mae_R"), "exit_reason": position.get("exit_reason"),
                                  "duration_seconds": position.get("duration_seconds")})
