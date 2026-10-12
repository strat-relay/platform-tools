"""Research-only V2 target selection for Context Structure Retrace.

V1 remains the authority for setup detection, stop placement, and the structural target
candidate generation.  V2 only changes target eligibility: it requires at least 1.0 planned R
and never invents a target when the structural candidates do not provide it.
"""
from __future__ import annotations

import os
from typing import Any

from context_structure_retrace_forward import _geometry as v1_geometry
from context_structure_retrace_v2_config import DEFAULTS, PARAMETER_FIELDS, PARAMETER_SCHEMA_ID
from strategy_backtest.models import fingerprint

STRATEGY_ID = "CONTEXT_STRUCTURE_RETRACE_V2"
VERSION = "V2"
MIN_PLANNED_R = 1.0

# These are the stable identities of the V2 research contract and parameter
# values.  They are intentionally independent of the source commit so a report
# can distinguish semantic drift from a rebuild of the same contract.
V2_CONTRACT_HASH = "4430542fb8d249d6338ead1fb745664a2e44836c4123e16f069b0c48bd69e107"
V2_PARAMETER_HASH = "ad897dff76e5b119d52fe7f05203d25d214b61c1dff69be18b9e8f784284335f"


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
                           ParameterSchema(PARAMETER_SCHEMA_ID, PARAMETER_FIELDS), lifecycle="RESEARCH_ONLY")


def research_parameter_set() -> Any:
    from strategy_backtest.models import ParameterSet
    return ParameterSet("context-v2-research-default", f"{STRATEGY_ID}@{VERSION}",
                        PARAMETER_SCHEMA_ID, dict(DEFAULTS),
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


def select_target_candidates(base: dict[str, Any], direction: str, entry: float,
                             policy: str = "NEAREST_VALID_STRUCTURE") -> list[dict[str, Any]]:
    extension = {"target": float(base["extension_target"]), "source": "STRUCTURE_CAPPED_EXTENSION"}
    opposing = base.get("opposing_structure")
    structure = ([{"target": float(opposing), "source": "OPPOSING_STRUCTURE"}]
                 if opposing is not None else [])
    if policy == "EXTENSION_ONLY":
        candidates = [extension]
    elif policy == "OPPOSING_STRUCTURE_ONLY":
        candidates = structure
    else:
        candidates = [extension, *structure]
    if direction == "LONG":
        return [row for row in candidates if row["target"] > entry]
    return [row for row in candidates if row["target"] < entry]


def apply_v2_target_policy(base: dict[str, Any], direction: str, entry: float,
                           *, minimum_required_r: float = MIN_PLANNED_R,
                           policy: str = "NEAREST_VALID_STRUCTURE") -> dict[str, Any]:
    """Apply V2 planned-R admission to live forward geometry as well as research geometry."""
    risk = float(base.get("stop_distance") or 0)
    candidates = select_target_candidates(base, direction, entry, policy)
    for candidate in candidates:
        signed = ((candidate["target"] - entry) if direction == "LONG"
                  else (entry - candidate["target"]))
        candidate.update({"reward_distance": signed, "planned_r": signed / risk if risk > 0 else None})
    eligible = [row for row in candidates if risk > 0 and row["planned_r"] >= minimum_required_r]
    selected = (min(eligible, key=lambda row: row["target"]) if direction == "LONG"
                else max(eligible, key=lambda row: row["target"])) if eligible else None
    result = {**base, "target_candidates": candidates,
              "rejected_target_candidates": [row for row in candidates if row is not selected],
              "minimum_required_r": minimum_required_r}
    if selected is None:
        best = max(candidates, key=lambda row: row["planned_r"] or float("-inf")) if candidates else None
        result.update({"v2_eligible": False,
                       "rejection_reason": "INVALID_RISK" if risk <= 0 else "RR_BELOW_MINIMUM",
                       "best_structural_target": best["target"] if best else None,
                       "best_structural_target_r": best["planned_r"] if best else None,
                       "effective_target": entry, "signed_target_distance": 0.0,
                       "target_direction_state": "TARGET_AT_ENTRY", "target_R": 0.0})
        return result
    result.update({"v2_eligible": True, "effective_target": selected["target"],
                   "signed_target_distance": selected["reward_distance"],
                   "target_R": selected["planned_r"],
                   "target_source": selected["source"], "target_structure": selected["source"],
                   "rejection_reason": None})
    return result


def v2_geometry(event_bar: dict[str, Any], direction: str, snapshot: dict[str, Any], entry: float,
                spread: float, atr_value: float | None, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    base = v1_geometry(event_bar, direction, snapshot, entry, spread, atr_value)
    return apply_v2_target_policy(
        base, direction, entry,
        minimum_required_r=float((parameters or {}).get("minimum_required_r", MIN_PLANNED_R)),
    )
