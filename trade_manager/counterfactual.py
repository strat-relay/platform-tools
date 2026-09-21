"""Separate hypothetical managed-position ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metrics import mfe_surrender


@dataclass
class CounterfactualPosition:
    economic_position_id: str
    frozen: dict[str, Any]
    hypothetical: dict[str, Any] = field(default_factory=dict)
    stop_movements: list[dict[str, Any]] = field(default_factory=list)
    reductions: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    additions: list[dict[str, Any]] = field(default_factory=list)
    reentries: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, observation: dict[str, Any]) -> None:
        """Update only the hypothetical ledger; never the frozen position."""
        self.hypothetical.update({"current_R": observation.get("current_R"),
                                  "MFE_R": observation.get("mfe_R"), "MAE_R": observation.get("mae_R"),
                                  "duration_seconds": observation.get("time_in_trade_seconds"),
                                  **mfe_surrender(observation.get("mfe_R"), observation.get("current_R"))})

    def apply_shadow(self, decision: dict[str, Any], proposal: dict[str, Any] | None = None) -> None:
        action = decision.get("action")
        if proposal and action == "TRAIL_STOP":
            self.stop_movements.append(proposal); self.hypothetical["current_stop"] = proposal["proposed_stop"]
        elif action == "REDUCE_POSITION":
            self.reductions.append(decision)
        elif action == "CLOSE_POSITION":
            self.exits.append(decision); self.hypothetical["status"] = "CLOSED"
        elif action == "ADD_POSITION":
            self.additions.append({**decision, "portfolio_authorization_required": True})
        elif action == "REENTRY_ELIGIBLE":
            self.reentries.append({**decision, "portfolio_authorization_required": True})

    def comparison(self) -> dict[str, Any]:
        frozen_r = self.frozen.get("realized_R")
        managed_r = self.hypothetical.get("realized_R")
        return {"economic_position_id": self.economic_position_id, "frozen": self.frozen,
                "managed_hypothetical": {**self.hypothetical, "stop_movements": self.stop_movements,
                                          "reductions": self.reductions, "exits": self.exits,
                                          "additions": self.additions, "reentries": self.reentries},
                "MFE_captured_R": self.hypothetical.get("mfe_captured_R"),
                "MFE_surrender_R": self.hypothetical.get("mfe_surrender_R"),
                "delta_vs_frozen_R": None if frozen_r is None or managed_r is None else managed_r - frozen_r,
                "interpretation": "COUNTERFACTUAL_ONLY_NOT_A_PERFORMANCE_CLAIM"}
