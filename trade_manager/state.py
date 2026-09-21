"""Restart-safe advisory management state."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATES = ("UNMANAGED", "INITIAL_RISK", "PROFIT_DEVELOPING", "PROTECTION_ELIGIBLE",
          "PROTECTED", "TRAILING", "REDUCED", "EXIT_RECOMMENDED", "CLOSED")


@dataclass
class ManagementState:
    economic_position_id: str
    state: str = "UNMANAGED"
    policy_id: str | None = None
    policy_version: str | None = None
    updated_at: str | None = None

    def transition(self, decision: dict[str, Any], timestamp: str) -> str:
        action = decision.get("action")
        if decision.get("reason_codes") == ["POSITION_CLOSED"]:
            self.state = "CLOSED"
        elif action in {"PROTECT_STOP", "MOVE_BREAKEVEN"}:
            self.state = "PROTECTED"
        elif action == "TRAIL_STOP":
            self.state = "TRAILING"
        elif action == "REDUCE_POSITION":
            self.state = "REDUCED"
        elif action == "CLOSE_POSITION":
            self.state = "EXIT_RECOMMENDED"
        elif float(decision.get("current_R") or 0) > 0:
            self.state = "PROFIT_DEVELOPING"
        else:
            self.state = "INITIAL_RISK"
        self.updated_at = timestamp
        return self.state


class ManagementStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.states: dict[str, ManagementState] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line); self.states[row["economic_position_id"]] = ManagementState(**row)

    def update(self, state: ManagementState) -> None:
        self.states[state.economic_position_id] = state
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(state.__dict__, sort_keys=True) + "\n")
