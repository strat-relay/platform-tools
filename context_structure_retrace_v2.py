"""Research-only V2 target selection for Context Structure Retrace.

V1 remains the authority for setup detection, stop placement, and the structural target
candidate generation.  V2 only changes target eligibility: it requires at least 1.0 planned R
and never invents a target when the structural candidates do not provide it.
"""
from __future__ import annotations

import os
from typing import Any

from context_structure_retrace_forward import _geometry as v1_geometry
from strategy_backtest.models import fingerprint

STRATEGY_ID = "CONTEXT_STRUCTURE_RETRACE_V2"
VERSION = "V2"
MIN_PLANNED_R = 1.0

# These are the stable identities of the V2 research contract and parameter
# values.  They are intentionally independent of the source commit so a report
# can distinguish semantic drift from a rebuild of the same contract.
V2_CONTRACT_HASH = "4430542fb8d249d6338ead1fb745664a2e44836c4123e16f069b0c48bd69e107"
V2_PARAMETER_HASH = "dc72c5d03e547fc02e1c80c91fb244e2b153a0bd32a8b9a8f3df200c71a61394"


def research_contract() -> dict[str, Any]:
    """Return the non-routing contract for the V2 research/shadow experiment."""
    return {"strategy_id": STRATEGY_ID, "strategy_version": VERSION,
            "evaluator_key": "context_structure_retrace_v2_research",
            "execution_timeframe": "M15", "lower_context": ["M5"],
            "higher_context": ["H1", "H4"], "minimum_required_r": MIN_PLANNED_R,
            "lifecycle": "RESEARCH_ONLY", "broker_writes": False}


def research_strategy_version() -> Any:
    from strategy_backtest.models import ParameterSchema, StrategyVersion
    return StrategyVersion(STRATEGY_ID, VERSION, "context_structure_retrace_v2_research",
                           ParameterSchema("context-structure-retrace-v2-research-v1", {
                               "max_hold_minutes": {"required": True, "minimum": 1},
                           }), lifecycle="RESEARCH_ONLY")


def research_parameter_set() -> Any:
    from strategy_backtest.models import ParameterSet
    return ParameterSet("context-v2-research-default", f"{STRATEGY_ID}@{VERSION}",
                        "context-structure-retrace-v2-research-v1", {"max_hold_minutes": 1440},
                        {"research_only": True, "broker_writes": False})


def research_metadata() -> dict[str, Any]:
    """Return read-only identity metadata for shadow/research reports."""
    contract = research_contract()
    parameter_set = research_parameter_set()
    parameter_hash = fingerprint(dict(parameter_set.values))
    if fingerprint(contract) != V2_CONTRACT_HASH or parameter_hash != V2_PARAMETER_HASH:
        raise RuntimeError("Context V2 research identity drifted from its pinned fingerprints")
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": VERSION,
        "contract_hash": V2_CONTRACT_HASH,
        "parameter_hash": V2_PARAMETER_HASH,
        "source_commit": os.getenv("SOURCE_COMMIT") or os.getenv("P2_SHADOW_COMMIT") or "UNSET",
        "lifecycle": "RESEARCH_ONLY",
        "broker_writes": False,
    }


def select_target_candidates(base: dict[str, Any], direction: str, entry: float) -> list[dict[str, Any]]:
    candidates = [{"target": float(base["extension_target"]), "source": "STRUCTURE_CAPPED_EXTENSION"}]
    opposing = base.get("opposing_structure")
    if opposing is not None:
        candidates.append({"target": float(opposing), "source": "OPPOSING_STRUCTURE"})
    if direction == "LONG":
        return [row for row in candidates if row["target"] > entry]
    return [row for row in candidates if row["target"] < entry]


def v2_geometry(event_bar: dict[str, Any], direction: str, snapshot: dict[str, Any], entry: float,
                spread: float, atr_value: float | None) -> dict[str, Any]:
    base = v1_geometry(event_bar, direction, snapshot, entry, spread, atr_value)
    risk = float(base["stop_distance"])
    if risk <= 0:
        return {**base, "target_candidates": [], "rejected_target_candidates": [],
                "minimum_required_r": MIN_PLANNED_R, "v2_eligible": False,
                "best_structural_target": None, "best_structural_target_r": None,
                "rejection_reason": "INVALID_RISK"}
    candidates = select_target_candidates(base, direction, entry)
    for candidate in candidates:
        signed = (candidate["target"] - entry) if direction == "LONG" else (entry - candidate["target"])
        candidate.update({"reward_distance": signed, "planned_r": signed / risk})
    eligible = [row for row in candidates if row["planned_r"] >= MIN_PLANNED_R]
    # Preserve V1's nearest-structural-target hierarchy among candidates that satisfy V2.
    selected = (min(eligible, key=lambda row: row["target"]) if direction == "LONG"
                else max(eligible, key=lambda row: row["target"])) if eligible else None
    rejected = [row for row in candidates if selected is None or row["target"] != selected["target"]]
    result = {**base, "target_candidates": candidates, "rejected_target_candidates": rejected,
              "minimum_required_r": MIN_PLANNED_R}
    if selected is None:
        result.update({"v2_eligible": False, "rejection_reason": "RR_BELOW_MINIMUM",
                       "best_structural_target": (max(candidates, key=lambda row: row["planned_r"])["target"]
                                                   if candidates else None),
                       "best_structural_target_r": (max(candidates, key=lambda row: row["planned_r"])["planned_r"]
                                                     if candidates else None)})
        return result
    signed = float(selected["reward_distance"])
    result.update({"v2_eligible": True, "effective_target": selected["target"],
                   "signed_target_distance": signed, "target_R": signed / risk,
                   "target_source": selected["source"], "target_structure": selected["source"],
                   "rejection_reason": None})
    return result
