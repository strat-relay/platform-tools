"""Pure custom trailing evaluators. They return proposals only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import structural_stop


@dataclass(frozen=True)
class TrailingProposal:
    action: str
    old_stop: float
    proposed_stop: float
    current_price: float
    locked_R: float | None
    mfe_R: float
    method: str
    evidence: dict[str, Any]
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "old_stop": self.old_stop, "proposed_stop": self.proposed_stop,
                "current_price": self.current_price, "locked_R": self.locked_R, "MFE_R": self.mfe_R,
                "method": self.method, "evidence": self.evidence, "reason_codes": list(self.reason_codes),
                "authorization_mode": "ADVISORY_SHADOW", "advisory_only": True}


def _improves(direction: str, old_stop: float, proposed: float) -> bool:
    return proposed > old_stop if direction.upper() == "LONG" else proposed < old_stop


def _proposal(position: dict[str, Any], observation: dict[str, Any], proposed: float,
              method: str, locked_r: float | None, evidence: dict[str, Any]) -> TrailingProposal | None:
    old_stop = float(position.get("current_stop", position["original_stop"]))
    proposed = float(proposed)
    if not _improves(position["direction"], old_stop, proposed):
        return None
    return TrailingProposal("TRAIL_STOP", old_stop, proposed, float(observation["close_executable_price"]),
                            locked_r, float(observation.get("mfe_R", 0.0)), method, evidence,
                            ("TRAIL_ELIGIBLE", "STOP_IMPROVEMENT"))


def r_based(position: dict[str, Any], observation: dict[str, Any], config: dict[str, Any]) -> TrailingProposal | None:
    activation = config.get("activation_r"); locked = config.get("locked_r")
    if not config.get("enabled") or activation is None or locked is None or float(observation.get("current_R", 0)) < float(activation):
        return None
    risk = abs(float(position["entry"]) - float(position["original_stop"]))
    proposed = float(position["entry"]) + float(locked) * risk if position["direction"].upper() == "LONG" else float(position["entry"]) - float(locked) * risk
    return _proposal(position, observation, proposed, "R_BASED", float(locked), {"activation_r": activation, "risk_distance": risk})


def structure(position: dict[str, Any], observation: dict[str, Any], config: dict[str, Any]) -> TrailingProposal | None:
    if not config.get("enabled"):
        return None
    s = observation.get("market", {}).get("structure", {})
    level = s.get("latest_confirmed_swing_low") if position["direction"].upper() == "LONG" else s.get("latest_confirmed_swing_high")
    if not level:
        return None
    proposed = structural_stop(position["direction"], float(level["price"]), float(config.get("buffer", 0.0)))
    return _proposal(position, observation, proposed, "STRUCTURE", None, {"structure_level": level, "buffer": config.get("buffer", 0.0)})


def ema_structure(position: dict[str, Any], observation: dict[str, Any], config: dict[str, Any]) -> TrailingProposal | None:
    if not config.get("enabled"):
        return None
    ema = observation.get("market", {}).get("M5_EMA", {}).get("value")
    if ema is None:
        return None
    candidate = structure(position, observation, config)
    if candidate is None:
        return None
    # Keep the candidate on the protective side of both the EMA and structure.
    buffer = abs(float(config.get("buffer", 0.0)))
    if position["direction"].upper() == "LONG":
        proposed = min(candidate.proposed_stop, float(ema) - buffer)
    else:
        proposed = max(candidate.proposed_stop, float(ema) + buffer)
    return _proposal(position, observation, proposed, "EMA_STRUCTURE", None,
                     {**candidate.evidence, "ema_200": ema, "buffer": buffer})


def atr_structure(position: dict[str, Any], observation: dict[str, Any], config: dict[str, Any]) -> TrailingProposal | None:
    if not config.get("enabled") or observation.get("atr") is None:
        return None
    cfg = dict(config); cfg["buffer"] = float(observation["atr"]) * float(config.get("atr_multiple", 1.0))
    proposal = structure(position, observation, cfg)
    if proposal:
        proposal = TrailingProposal(proposal.action, proposal.old_stop, proposal.proposed_stop, proposal.current_price,
                                    proposal.locked_R, proposal.mfe_R, "ATR_STRUCTURE",
                                    {**proposal.evidence, "atr": observation["atr"], "atr_multiple": config.get("atr_multiple", 1.0)}, proposal.reason_codes)
    return proposal


METHODS = {"R_BASED": r_based, "STRUCTURE": structure, "EMA_STRUCTURE": ema_structure, "ATR_STRUCTURE": atr_structure}


def evaluate_trailing(position: dict[str, Any], observation: dict[str, Any], config: dict[str, Any]) -> TrailingProposal | None:
    method = config.get("method")
    if method not in METHODS:
        return None
    return METHODS[method](position, observation, config)
