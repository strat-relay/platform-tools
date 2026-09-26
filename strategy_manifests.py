"""Strategy parameter manifests, derived from the code the runtimes execute.

A StrategyVersion manifest classifies every piece of configuration:

  VERSION_OWNED_IMMUTABLE  frozen semantics/config of the version (changing it is a new version)
  INSTANCE_CONFIGURABLE    per-instance ParameterSet fields; a ParameterSet is frozen, so changing
                           a value means a new ParameterSet (new parameter_set_id and fingerprint)
  RUNTIME_OPERATIONAL      platform state (ONLINE/OFFLINE, instrument membership), changed via
                           the Control API and re-read by runtimes every cycle

Nothing here is authored in the database: scripts/publish_strategy_manifests.py publishes these
manifests so the Control API can render configuration without importing strategy code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import Any

MANIFEST_SCHEMA_VERSION = "strategy-parameter-manifest.v1"
VERSION_OWNED = "VERSION_OWNED_IMMUTABLE"
INSTANCE_CONFIGURABLE = "INSTANCE_CONFIGURABLE"

# Parameter sets live in strategy code and are loaded by runtimes at process start.
EDIT_POLICY = {
    "instance_parameters": "NEW_PARAMETER_SET_REQUIRED",
    "reason": ("ParameterSets are frozen in strategy code and identified by parameter_set_id + "
               "config_fingerprint; runtimes load them at start. Changing a value is a new ParameterSet "
               "(a reviewed code change and deploy), never an in-place edit."),
    "config_reload": "RESTART_REQUIRED",
}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _manifest(strategy_id: str, version: str, parameters: list[dict[str, Any]],
              immutable: list[dict[str, Any]], code_fingerprint: str | None) -> dict[str, Any]:
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "strategy_id": strategy_id, "strategy_version": version,
            "code_fingerprint": code_fingerprint, "parameters": parameters, "immutable": immutable,
            "edit_policy": EDIT_POLICY}


def liquidity_manifests() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from orchestration.liquidity_instances import LiquidityInstanceDefinition
    from orchestration.liquidity_live import (CODE_FINGERPRINT, PARAMETER_SCHEMA, PARAMETER_SETS,
                                              STRATEGY_ID, STRATEGY_VERSION)
    defaults = {f.name: f.default for f in fields(LiquidityInstanceDefinition)}
    parameters = [{**field, "classification": INSTANCE_CONFIGURABLE, "editable": False} for field in PARAMETER_SCHEMA]
    immutable = [
        {"key": "entry_semantics", "label": "Entry model", "value": defaults["entry_semantics"],
         "classification": VERSION_OWNED},
        {"key": "stop_semantics", "label": "Stop model", "value": defaults["stop_semantics"],
         "classification": VERSION_OWNED},
    ]
    manifest = _manifest(STRATEGY_ID, STRATEGY_VERSION, parameters, immutable, CODE_FINGERPRINT)
    sets = [{"instance_id": p.instance_id, "strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION,
             "parameter_set_id": p.parameter_set_id, "config_fingerprint": p.config_fingerprint,
             "parameters": {k: getattr(p, k) for k in (f["key"] for f in PARAMETER_SCHEMA)}}
            for p in PARAMETER_SETS.values()]
    return manifest, sets


def context_manifests() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import context_structure_retrace_forward as ctx
    immutable = [{"key": k, "label": k.replace("_", " ").capitalize(), "value": v, "classification": VERSION_OWNED}
                 for k, v in sorted(ctx.FROZEN_CONFIG.items())]
    manifest = _manifest(ctx.VERSION, "V1", [], immutable, ctx.FROZEN_DECISION_CODE_HASH)
    # The frozen configuration is the whole ParameterSet; phase6 has no instance-configurable fields.
    sets = [{"instance_id": "phase6", "strategy_id": ctx.VERSION, "strategy_version": "V1",
             "parameter_set_id": "context-v1-frozen", "config_fingerprint": ctx.config_hash(), "parameters": {}}]
    return manifest, sets


def all_manifests() -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    return [context_manifests(), liquidity_manifests()]
