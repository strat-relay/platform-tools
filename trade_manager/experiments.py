"""Isolated multi-policy counterfactual management experiments."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from statistics import median
from typing import Any
from datetime import datetime

from .metrics import mfe_surrender
from .policies import context_v1_experiment
from .semantics import price_semantics
from .trailing import evaluate_trailing


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    policy_id: str
    policy_version: str
    parameter_set: str
    kind: str
    parameters: dict[str, Any] = field(default_factory=dict)
    experimental: bool = True


def default_experiments() -> list[ExperimentSpec]:
    base = context_v1_experiment().policy_id
    return [
        ExperimentSpec("CONTROL_FROZEN", "CONTROL_FROZEN", "1", "original_strategy_management", "CONTROL", {}),
        ExperimentSpec("BE_PROTECTION_0_50R", base, "1", "be_0.50R_to_0R", "BE", {"activation_r": .50, "protected_r": 0.0}),
        ExperimentSpec("BE_PROTECTION_0_75R", base, "1", "be_0.75R_to_0R", "BE", {"activation_r": .75, "protected_r": 0.0}),
        ExperimentSpec("BE_PROTECTION_1_00R", base, "1", "be_1.00R_to_0R", "BE", {"activation_r": 1.0, "protected_r": 0.0}),
        ExperimentSpec("R_TRAILING_EXPERIMENT", base, "1", "r_trail_parameter_set_1", "R_TRAIL", {"activation_r": .75, "locked_r": .25, "minimum_improvement_r": .10}),
        ExperimentSpec("STRUCTURE_TRAILING_EXPERIMENT", base, "1", "structure_after_0.75R", "STRUCTURE", {"activation_r": .75, "buffer": 0.0}),
        ExperimentSpec("EMA_STRUCTURE_EXPERIMENT", base, "1", "ema200_plus_structure", "EMA_STRUCTURE", {"activation_r": .75, "buffer": 0.0}),
        ExperimentSpec("HYBRID_PROTECTION_EXPERIMENT", base, "1", "be_then_structure", "HYBRID", {"be_activation_r": .75, "structure_activation_r": 1.0, "protected_r": 0.0, "buffer": 0.0}),
    ]


@dataclass
class HypotheticalPosition:
    experiment: ExperimentSpec
    economic_position_id: str
    original_stop: float
    target: float
    hypothetical_stop: float
    status: str = "OPEN"
    exit_reason: str | None = None
    realized_R: float | None = None
    exit_price: float | None = None
    stop_movements: list[dict[str, Any]] = field(default_factory=list)
    mfe_R: float = 0.0
    mae_R: float = 0.0
    maximum_locked_R: float = 0.0
    duration_seconds: float | None = None

    def position_view(self, position: dict[str, Any]) -> dict[str, Any]:
        return {**position, "current_stop": self.hypothetical_stop, "mfe_R": self.mfe_R, "mae_R": self.mae_R,
                "status": self.status}


class MultiPolicyExperimentRunner:
    def __init__(self, experiments: list[ExperimentSpec] | None = None):
        self.experiments = experiments or default_experiments()
        self.positions: dict[tuple[str, str], HypotheticalPosition] = {}
        self.results: list[dict[str, Any]] = []

    def _new(self, position: dict[str, Any], spec: ExperimentSpec) -> HypotheticalPosition:
        return HypotheticalPosition(spec, position["economic_position_id"], float(position["original_stop"]),
                                    float(position["original_target"]), float(position["original_stop"]))

    def restore(self, position: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        """Restore terminal/hypothetical stop state from the append-only result stream."""
        pid = position["economic_position_id"]
        latest = {}
        for row in rows:
            if row.get("economic_position_id") == pid:
                latest[row.get("management_experiment_id")] = row
        for spec in self.experiments:
            row = latest.get(spec.experiment_id)
            if not row:
                continue
            state = self.positions.setdefault((pid, spec.experiment_id), self._new(position, spec))
            state.hypothetical_stop = float(row.get("hypothetical_stop", state.hypothetical_stop))
            state.status = row.get("status", state.status); state.realized_R = row.get("realized_R", state.realized_R)
            state.exit_reason = row.get("exit_reason", state.exit_reason); state.mfe_R = float(row.get("MFE_R", state.mfe_R) or 0)
            state.mae_R = float(row.get("MAE_R", state.mae_R) or 0); state.maximum_locked_R = float(row.get("maximum_locked_R", state.maximum_locked_R) or 0)
            state.stop_movements = list(row.get("stop_movements_detail", state.stop_movements) or [])

    @staticmethod
    def _r(direction: str, entry: float, price: float, risk: float) -> float:
        return ((price - entry) if direction.upper() == "LONG" else (entry - price)) / risk

    def observe(self, position: dict[str, Any], observation: dict[str, Any]) -> list[dict[str, Any]]:
        pid = position["economic_position_id"]
        out = []
        for spec in self.experiments:
            key = (pid, spec.experiment_id)
            state = self.positions.setdefault(key, self._new(position, spec))
            if state.status == "CLOSED":
                continue
            entry = float(position["entry"]); risk = abs(entry - float(position["original_stop"]))
            state.mfe_R = max(state.mfe_R, float(observation.get("mfe_R", 0.0)))
            state.mae_R = max(state.mae_R, float(observation.get("mae_R", 0.0)))
            sem = price_semantics(position["direction"], float(observation["bid"]), float(observation["ask"]))
            stop_px = sem.stop_cross_price; target_px = sem.target_cross_price
            close_r = self._r(position["direction"], entry, sem.close_price, risk)
            if (position["direction"].upper() == "LONG" and stop_px <= state.hypothetical_stop) or (position["direction"].upper() == "SHORT" and stop_px >= state.hypothetical_stop):
                state.status, state.exit_reason, state.exit_price, state.realized_R = "CLOSED", "HYPOTHETICAL_STOP", state.hypothetical_stop, self._r(position["direction"], entry, state.hypothetical_stop, risk)
            elif (position["direction"].upper() == "LONG" and target_px >= state.target) or (position["direction"].upper() == "SHORT" and target_px <= state.target):
                state.status, state.exit_reason, state.exit_price, state.realized_R = "CLOSED", "HYPOTHETICAL_TARGET", state.target, self._r(position["direction"], entry, state.target, risk)
            proposal = None
            if state.status == "OPEN" and spec.kind != "CONTROL":
                cfg = self._trailing_config(spec, observation)
                if cfg:
                    proposal = evaluate_trailing({**position, "current_stop": state.hypothetical_stop}, observation, cfg)
                if spec.kind == "BE" and state.status == "OPEN" and float(observation.get("current_R", 0)) >= float(spec.parameters["activation_r"]):
                    protected = entry + float(spec.parameters["protected_r"]) * risk if position["direction"].upper() == "LONG" else entry - float(spec.parameters["protected_r"]) * risk
                    if (position["direction"].upper() == "LONG" and protected > state.hypothetical_stop) or (position["direction"].upper() == "SHORT" and protected < state.hypothetical_stop):
                        proposal = {"old_stop": state.hypothetical_stop, "proposed_stop": protected, "method": "BREAKEVEN_EXPERIMENT", "locked_R": spec.parameters["protected_r"], "reason_codes": ["BREAKEVEN_ELIGIBLE"]}
            if proposal and state.status == "OPEN":
                proposed = float(proposal["proposed_stop"])
                improves = proposed > state.hypothetical_stop if position["direction"].upper() == "LONG" else proposed < state.hypothetical_stop
                if improves:
                    proposal_row = proposal.as_dict() if hasattr(proposal, "as_dict") else proposal
                    state.stop_movements.append({"timestamp": observation["timestamp"], **proposal_row})
                    state.hypothetical_stop = proposed
                    state.maximum_locked_R = max(state.maximum_locked_R, float(proposal_row.get("locked_R") or self._r(position["direction"], entry, proposed, risk)))
            state.duration_seconds = observation.get("time_in_trade_seconds")
            metrics = mfe_surrender(state.mfe_R, close_r)
            frozen_realized = position.get("realized_R")
            classification = None
            if state.status == "CLOSED" and state.realized_R is not None and frozen_realized is not None:
                if float(frozen_realized) > 0 and float(state.realized_R) < float(frozen_realized):
                    classification = "FROZEN_WINNER_PREMATURELY_EXITED"
                elif float(frozen_realized) <= 0 and float(state.realized_R) > float(frozen_realized):
                    classification = "FROZEN_LOSER_PROTECTED"
                elif float(state.realized_R) < float(frozen_realized):
                    classification = "MANAGEMENT_WORSE_R"
                elif float(state.realized_R) > float(frozen_realized):
                    classification = "MANAGEMENT_HIGHER_R"
                else:
                    classification = "FROZEN_RESULT_UNCHANGED"
            result = {"timestamp": observation["timestamp"], "economic_position_id": pid,
                      "management_experiment_id": spec.experiment_id, "policy_id": spec.policy_id,
                      "policy_version": spec.policy_version, "parameter_set": spec.parameter_set,
                      "status": state.status, "action": "HOLD" if not proposal else "PROPOSE_STOP",
                      "hypothetical_stop": state.hypothetical_stop, "realized_R": state.realized_R,
                      "current_R": close_r, "MFE_R": state.mfe_R, "MAE_R": state.mae_R,
                      **metrics, "duration_seconds": state.duration_seconds,
                      "exit_reason": state.exit_reason, "stop_movements": len(state.stop_movements),
                      "stop_movements_detail": state.stop_movements,
                      "maximum_locked_R": state.maximum_locked_R, "advisory_only": True,
                      "authorization_mode": "ADVISORY_SHADOW", "frozen_realized_R": frozen_realized,
                      "comparison_classification": classification}
            self.results.append(result); out.append(result)
        return out

    def _trailing_config(self, spec: ExperimentSpec, observation: dict[str, Any]) -> dict[str, Any] | None:
        if spec.kind == "R_TRAIL":
            return {"enabled": True, "method": "R_BASED", "activation_r": spec.parameters["activation_r"], "locked_r": spec.parameters["locked_r"]}
        if spec.kind == "STRUCTURE" and float(observation.get("current_R", 0)) >= spec.parameters["activation_r"]:
            return {"enabled": True, "method": "STRUCTURE", "buffer": spec.parameters["buffer"]}
        if spec.kind == "EMA_STRUCTURE" and float(observation.get("current_R", 0)) >= spec.parameters["activation_r"]:
            return {"enabled": True, "method": "EMA_STRUCTURE", "buffer": spec.parameters["buffer"]}
        if spec.kind == "HYBRID":
            if float(observation.get("current_R", 0)) >= spec.parameters["structure_activation_r"]:
                return {"enabled": True, "method": "STRUCTURE", "buffer": spec.parameters["buffer"]}
        return None

    def summary(self) -> list[dict[str, Any]]:
        grouped = {}
        for row in self.results:
            grouped.setdefault((row["policy_id"], row["policy_version"], row["parameter_set"]), []).append(row)
        summaries = []
        for (policy, version, params), rows in grouped.items():
            completed = [r for r in rows if r["status"] == "CLOSED"]
            rs = [float(r["realized_R"]) for r in completed if r["realized_R"] is not None]
            wins = [r for r in rs if r > 0]; losses = [r for r in rs if r < 0]
            curve = []; peak = 0.0; drawdown = 0.0
            for value in rs:
                curve.append(value); peak = max(peak, sum(curve)); drawdown = max(drawdown, peak - sum(curve))
            summaries.append({"policy_id": policy, "policy_version": version, "parameter_set": params,
                              "N": len(completed), "total_R": sum(rs), "mean_R": sum(rs) / len(rs) if rs else None,
                              "median_R": median(rs) if rs else None, "win_rate": len(wins) / len(rs) if rs else None,
                              "average_winner_R": sum(wins) / len(wins) if wins else None,
                              "average_loser_R": sum(losses) / len(losses) if losses else None,
                              "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
                              "max_counterfactual_drawdown_R": drawdown,
                              "MFE_capture_mean": sum((r["mfe_capture_ratio"] for r in rows if r["mfe_capture_ratio"] is not None), 0) / max(1, sum(r["mfe_capture_ratio"] is not None for r in rows)),
                              "frozen_winners_prematurely_exited": sum(r.get("comparison_classification") == "FROZEN_WINNER_PREMATURELY_EXITED" for r in rows),
                              "frozen_losers_protected": sum(r.get("comparison_classification") == "FROZEN_LOSER_PROTECTED" for r in rows),
                              "stopped_at_breakeven_before_frozen_target": sum(r.get("realized_R") == 0 and float(r.get("frozen_realized_R") or 0) > 0 for r in rows),
                              "counterfactual_only": True})
        return summaries

    def comparison_for(self, economic_position_id: str) -> list[dict[str, Any]]:
        latest = {}
        for row in self.results:
            if row.get("economic_position_id") == economic_position_id:
                latest[row["management_experiment_id"]] = row
        return list(latest.values())

    def dashboard(self) -> dict[str, Any]:
        return {"mode": "ADVISORY_SHADOW", "active_trades": sum(s.status == "OPEN" for s in self.positions.values()),
                "observed_trades": len({r["economic_position_id"] for r in self.results}),
                "experiments": [{"experiment_id": x.experiment_id, "policy_id": x.policy_id,
                                 "policy_version": x.policy_version, "parameter_set": x.parameter_set,
                                 "experimental": x.experimental} for x in self.experiments],
                "summaries": self.summary(), "broker_writes": 0}
