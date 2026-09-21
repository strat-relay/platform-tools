"""Versioned, inactive Phase 6 restart-state projection.

This module deliberately does not change the active runner's STATE path.  It
projects the existing full state into a compact sidecar and provides offline
validation helpers for a later, explicitly approved cutover.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
FULL_STATE = ROOT / "context_structure_retrace_forward_state.json"
COMPACT_STATE = ROOT / "context_structure_retrace_forward_state_compact.json"
MANIFEST = ROOT / "context_structure_retrace_forward_manifest.json"
COHORT = ROOT / "context_structure_retrace_forward_cohort.json"

COMPACT_SCHEMA_VERSION = "context-structure-retrace-forward-compact-v1"
COHORT_SCHEMA_VERSION = "context-structure-retrace-forward-cohort-v1"

SYMBOL_PROGRESS_FIELDS = ("last_m5", "last_m15", "initialized", "last_candle")
SETUP_IDENTITY_FIELDS = (
    "setup_id", "market_event_id", "symbol", "direction", "pattern",
    "setup_timestamp", "setup_timestamp_iso", "qualification",
    "qualification_flags", "retrace_state", "entry_level",
    "theoretical_entry", "spread_at_detection", "target_completed", "status",
    "m5_start_index", "zone_left", "thesis_invalidated",
)
POSITION_FIELDS = (
    "entry_opportunity_id", "entry_attempt_id", "economic_position_id",
    "fill_timestamp", "fill_timestamp_iso", "fill_candle_number",
    "entry_mechanisms", "theoretical_entry", "executable_paper_entry",
    "spread_at_fill", "stop", "target", "status", "mfe_price", "mae_price",
    "reentry_type", "exit_timestamp", "exit_reason", "realized_R",
    "symbol", "direction", "setup_id", "pattern", "provenance",
)


def _pick(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: source[field] for field in fields if field in source}


def _compact_geometry(geometry: dict[str, Any] | None) -> dict[str, Any]:
    geometry = geometry or {}
    # stop_distance is required by the existing MFE/MAE reporting path;
    # target_R is required when an open position later reaches its target.
    return _pick(geometry, ("stop_distance", "target_R"))


def _compact_position(position: dict[str, Any]) -> dict[str, Any]:
    result = _pick(position, POSITION_FIELDS)
    result["geometry"] = _compact_geometry(position.get("geometry"))
    result["leg_a"] = _pick(position.get("leg_a") or {}, ("allocation_R", "status"))
    result["leg_b"] = _pick(position.get("leg_b") or {}, ("allocation_R", "breakeven_activated"))
    return result


def _compact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only fields read by _fill/_geometry after restart."""
    snapshot = snapshot or {}
    provenance = snapshot.get("provenance") or {}
    structure_timeframe = provenance.get("structure_timeframe")
    timeframes = snapshot.get("timeframes") or {}
    m15 = timeframes.get("M15") or {}
    structure = timeframes.get(structure_timeframe) or {}
    atr = (m15.get("ema_context") or {}).get("atr")
    zones = []
    for zone in (structure.get("sr_context") or {}).get("zones", []):
        zones.append(_pick(zone, ("support_resistance_role", "zone_low", "zone_high")))
    compact_timeframes: dict[str, Any] = {
        "M15": {"ema_context": {"atr": atr}},
    }
    if structure_timeframe:
        compact_timeframes.setdefault(structure_timeframe, {})["sr_context"] = {"zones": zones}
    return {
        "schema": "context-structure-retrace-compact-snapshot-v1",
        "provenance": {"structure_timeframe": structure_timeframe},
        "timeframes": compact_timeframes,
    }


def _compact_setup(setup: dict[str, Any]) -> dict[str, Any]:
    result = _pick(setup, SETUP_IDENTITY_FIELDS)
    result["event_bar"] = _pick(
        setup.get("event_bar") or {},
        ("time", "open", "high", "low", "close", "spread", "tick_volume", "real_volume"),
    )
    provenance = setup.get("provenance") or {}
    result["provenance"] = _pick(
        provenance,
        ("as_of_timestamp", "completed_candles_only", "future_ohlc_exposed", "source", "phase2_representation_hash"),
    )
    result["context_snapshot"] = _compact_snapshot(setup.get("context_snapshot"))
    result["opportunities"] = [_compact_position(position) for position in setup.get("opportunities", [])]
    return result


def project_state(source: dict[str, Any]) -> dict[str, Any]:
    """Create a compact state without mutating *source*."""
    # The nested setup opportunity is the single mutable lifecycle authority.
    # ``positions`` is retained only as a compatibility projection for readers
    # that need an economic-position lookup.  The old implementation copied a
    # second, independently mutable index and allowed it to drift terminal.
    derived_positions: dict[str, dict[str, Any]] = {}
    for setup in (source.get("setups") or {}).values():
        for opportunity in setup.get("opportunities", []):
            position_id = opportunity.get("economic_position_id")
            if position_id:
                derived_positions[str(position_id)] = _compact_position(opportunity)
    # Preserve orphan records for forensic visibility, but never let them
    # override a lifecycle record present under its setup.
    for position_id, position in (source.get("positions") or {}).items():
        if str(position_id) not in derived_positions:
            derived_positions[str(position_id)] = _compact_position(position)
    result: dict[str, Any] = {
        "schema": COMPACT_SCHEMA_VERSION,
        "source_schema": source.get("schema"),
        "source_state_sha256": source.get("source_state_sha256"),
        "strategy_version": source.get("strategy_version"),
        "manifest": source.get("manifest"),
        "created_at": source.get("created_at"),
        "last_poll_at": source.get("last_poll_at"),
        "last_successful_read_at": source.get("last_successful_read_at"),
        "poll_interval_seconds": source.get("poll_interval_seconds"),
        "symbols": {
            symbol: _pick(value, SYMBOL_PROGRESS_FIELDS)
            for symbol, value in (source.get("symbols") or {}).items()
        },
        "setups": {
            setup_id: _compact_setup(setup)
            for setup_id, setup in (source.get("setups") or {}).items()
        },
        "positions": derived_positions,
        "counters": dict(source.get("counters") or {}),
        "prospective_boundary": source.get("prospective_boundary"),
        "runner_status": source.get("runner_status"),
        "kill_switch": source.get("kill_switch"),
    }
    return result


def load_json(path: Path) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return value, (time.perf_counter() - started) * 1000.0


def write_compact(source_path: Path = FULL_STATE, output_path: Path = COMPACT_STATE) -> dict[str, Any]:
    source, load_ms = load_json(source_path)
    source["source_state_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    projected = project_state(source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(projected, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    save_ms = (time.perf_counter() - started) * 1000.0
    return {
        "source_path": str(source_path),
        "output_path": str(output_path),
        "source_bytes": source_path.stat().st_size,
        "compact_bytes": output_path.stat().st_size,
        "source_setup_count": len(source.get("setups", {})),
        "compact_setup_count": len(projected.get("setups", {})),
        "source_position_count": len(source.get("positions", {})),
        "compact_position_count": len(projected.get("positions", {})),
        "source_load_ms": load_ms,
        "compact_save_ms": save_ms,
    }


def _ids(state: dict[str, Any]) -> dict[str, set[str]]:
    setup_ids = set(state.get("setups", {}))
    position_ids = set(state.get("positions", {}))
    opportunity_ids = {
        str(opportunity["entry_opportunity_id"])
        for setup in state.get("setups", {}).values()
        for opportunity in setup.get("opportunities", [])
        if opportunity.get("entry_opportunity_id")
    }
    return {"setups": setup_ids, "positions": position_ids, "opportunities": opportunity_ids}


def _lifecycle_projection(state: dict[str, Any]) -> dict[str, Any]:
    return {
        setup_id: {
            field: setup.get(field)
            for field in ("status", "retrace_state", "target_completed", "zone_left", "thesis_invalidated", "m5_start_index")
        }
        for setup_id, setup in state.get("setups", {}).items()
    }


def _symbol_progress_projection(state: dict[str, Any]) -> dict[str, Any]:
    return {
        symbol: _pick(value, SYMBOL_PROGRESS_FIELDS)
        for symbol, value in (state.get("symbols") or {}).items()
    }


def validate_equivalence(source: dict[str, Any], compact: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    source_ids = _ids(source)
    compact_ids = _ids(compact)
    symbol_fields_equal = _symbol_progress_projection(source) == _symbol_progress_projection(compact)
    lifecycle_equal = _lifecycle_projection(source) == _lifecycle_projection(compact)
    target_reentry_equal = all(
        source.get("setups", {}).get(key, {}).get("target_completed") == value.get("target_completed")
        and source.get("setups", {}).get(key, {}).get("zone_left") == value.get("zone_left")
        and source.get("setups", {}).get(key, {}).get("thesis_invalidated") == value.get("thesis_invalidated")
        for key, value in compact.get("setups", {}).items()
    )
    position_state_equal = all(
        source.get("positions", {}).get(key, {}).get("status") == value.get("status")
        and source.get("positions", {}).get(key, {}).get("stop") == value.get("stop")
        and source.get("positions", {}).get(key, {}).get("target") == value.get("target")
        and source.get("positions", {}).get(key, {}).get("mfe_price") == value.get("mfe_price")
        and source.get("positions", {}).get(key, {}).get("mae_price") == value.get("mae_price")
        and source.get("positions", {}).get(key, {}).get("realized_R") == value.get("realized_R")
        for key, value in compact.get("positions", {}).items()
    )
    manifest_equal = True
    if manifest is not None:
        manifest_equal = (
            source.get("strategy_version") == manifest.get("strategy_version")
            and source.get("manifest") == compact.get("manifest")
            and source.get("prospective_boundary") == compact.get("prospective_boundary")
        )
    return {
        "setup_ids_equal": source_ids["setups"] == compact_ids["setups"],
        "position_ids_equal": source_ids["positions"] == compact_ids["positions"],
        "opportunity_ids_equal": source_ids["opportunities"] == compact_ids["opportunities"],
        "symbol_progress_equal": symbol_fields_equal,
        "lifecycle_equal": lifecycle_equal,
        "target_reentry_equal": target_reentry_equal,
        "position_state_equal": position_state_equal,
        "freeze_manifest_equal": manifest_equal,
        "all_equal": all((
            source_ids["setups"] == compact_ids["setups"],
            source_ids["positions"] == compact_ids["positions"],
            source_ids["opportunities"] == compact_ids["opportunities"],
            symbol_fields_equal, lifecycle_equal, target_reentry_equal,
            position_state_equal, manifest_equal,
        )),
    }


def write_two_symbol_cohort(path: Path = COHORT) -> dict[str, Any]:
    value = {
        "schema": COHORT_SCHEMA_VERSION,
        "strategy_version": "CONTEXT_STRUCTURE_RETRACE_V1",
        "status": "PREPARED_NOT_ACTIVE",
        "activation_required": True,
        "active_symbols": ["XAUUSDm", "EURUSDm"],
        "historical_symbols": ["BTCUSDm", "USDJPYm", "GBPUSDm", "USTECm", "USTEC_x100m"],
        "historical_state_preserved": True,
        "frozen_strategy_rules_changed": False,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Project and validate inactive Phase 6 compact restart state")
    sub = parser.add_subparsers(dest="command", required=True)
    project = sub.add_parser("project")
    project.add_argument("--source", type=Path, default=FULL_STATE)
    project.add_argument("--output", type=Path, default=COMPACT_STATE)
    validate = sub.add_parser("validate")
    validate.add_argument("--source", type=Path, default=FULL_STATE)
    validate.add_argument("--compact", type=Path, default=COMPACT_STATE)
    validate.add_argument("--manifest", type=Path, default=MANIFEST)
    cohort = sub.add_parser("prepare-cohort")
    cohort.add_argument("--output", type=Path, default=COHORT)
    args = parser.parse_args()
    if args.command == "project":
        print(json.dumps(write_compact(args.source, args.output), indent=2, sort_keys=True))
    elif args.command == "validate":
        source, _ = load_json(args.source); compact, _ = load_json(args.compact); manifest, _ = load_json(args.manifest)
        print(json.dumps(validate_equivalence(source, compact, manifest), indent=2, sort_keys=True))
    else:
        print(json.dumps(write_two_symbol_cohort(args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
