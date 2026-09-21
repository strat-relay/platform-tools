"""Strategy observability: HTTP-shaped dispatch over each strategy's own
standard reporting layer (context_structure_retrace_forward.build_standard_report,
liquidity_displacement_forward.build_standard_report, etc).

This module performs no metric computation of its own — every number comes
from the strategy module's own build_standard_report()/build_shadow_report(),
the same function the CLI `report` subcommand calls. This module only adds:
registry dispatch by strategy_instance_id, clean degraded responses for
strategies/instances with no live adapter, and — for liquidity only —
reading a non-default instance's state file directly rather than through
that instance's configure() (see LIQUIDITY_INSTANCES below). It never calls
configure() and never mutates any strategy module's globals, for any
instance, live or not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_state_from(path: Path) -> dict[str, Any]:
    """Replicates liquidity_displacement_forward.load_state()'s fallback and
    setdefault behavior exactly, for an explicit path, without touching that
    module's global STATE. One deliberate deviation: when the file does not
    exist, the empty-state default's "version" is {} rather than calling the
    base module's manifest() — manifest() reads module globals (CFG/SYMBOL)
    that reflect whatever the base engine currently is, which would
    misattribute that identity to a different, not-live instance.
    """
    if path.exists():
        s = json.loads(path.read_text(encoding="utf-8"))
    else:
        s = {"version": {}, "started_at": None, "running": False, "last_candle": None,
             "last_mt5_data_timestamp": None, "last_poll_timestamp": None, "signals": {},
             "fills": [], "closes": [], "checkpoints": [], "gold_symbols": [],
             "source_sha256_at_start": None, "checkpoint_thresholds": []}
    for key, default in {"signals": {}, "decision_telemetry": {}, "fills": [], "closes": [],
                          "checkpoints": [], "checkpoint_thresholds": [],
                          "last_successful_mt5_read": None, "last_read_error": None}.items():
        s.setdefault(key, default)
    return s


def _read_jsonl_from(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _event_rows_from(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _liquidity_instances() -> dict[str, dict[str, Any]]:
    """Static instance registry, built once at import time from module
    constants — never from calling configure(). Base-engine paths are that
    module's own defaults; the four entry_forward variants derive paths from
    the same prefix formula liquidity_displacement_entry_forward.configure()
    itself uses; v1_variant_a's paths are copied once from its own configure().
    """
    import liquidity_displacement_forward as base
    import liquidity_displacement_entry_forward as entry

    instances: dict[str, dict[str, Any]] = {}

    instances["LIQUIDITY_DISPLACEMENT_SCALP_V1"] = {
        "instance_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
        "family_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
        "symbol": base.SYMBOL,
        "label": "XAUUSD Base",
        "state": base.STATE, "events": base.EVENTS, "daily": base.DAILY,
        "stop_path": base.STOP,
    }

    for name, spec in entry.VARIANTS.items():
        prefix = f"liquidity_displacement_{name}"
        version = spec["version"]
        instances[version] = {
            "instance_id": version,
            "family_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
            "symbol": spec["symbol"],
            "label": spec["label"],
            "state": entry.ROOT / f"{prefix}_state.json",
            "events": entry.ROOT / f"{prefix}.jsonl",
            "daily": entry.ROOT / f"{prefix}_daily.jsonl",
            "stop_path": Path(f"/tmp/{prefix}.stop"),
        }

    instances["LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A"] = {
        "instance_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A",
        "family_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
        "symbol": "XAUUSDm",
        "label": "Variant A",
        "state": base.ROOT / "liquidity_displacement_v1_variant_a_state.json",
        "events": base.ROOT / "liquidity_displacement_v1_variant_a.jsonl",
        "daily": base.ROOT / "liquidity_displacement_v1_variant_a_daily.jsonl",
        "stop_path": Path("/tmp/liquidity-displacement-paper-v1-variant-a.stop"),
    }

    return instances


LIQUIDITY_INSTANCES: dict[str, dict[str, Any]] = _liquidity_instances()


def _liquidity_instance_live(meta: dict[str, Any]) -> bool:
    return meta["state"].exists()


def build_liquidity_instance_report(instance_id: str) -> dict[str, Any] | None:
    meta = LIQUIDITY_INSTANCES.get(instance_id)
    if meta is None or not _liquidity_instance_live(meta):
        return None
    import liquidity_displacement_forward as base
    state = _load_state_from(meta["state"])
    daily_records = _read_jsonl_from(meta["daily"])
    event_records = _event_rows_from(meta["events"])
    instance = {"instance_id": meta["instance_id"], "family_id": meta["family_id"],
                "symbol": meta["symbol"], "label": meta["label"], "stop_path": meta["stop_path"]}
    return base.build_standard_report(state, instance, daily_records=daily_records, event_records=event_records)


def build_context_report() -> dict[str, Any]:
    import context_structure_retrace_forward as m
    return m.build_standard_report()


def build_context_shadow_report() -> dict[str, Any]:
    import context_structure_retrace_phase7_observer as m
    return m.build_shadow_report()


STRATEGY_ADAPTERS: dict[str, Callable[[], dict[str, Any] | None]] = {
    "CONTEXT_STRUCTURE_RETRACE_V1": build_context_report,
}
for _instance_id in LIQUIDITY_INSTANCES:
    STRATEGY_ADAPTERS[_instance_id] = (lambda iid=_instance_id: build_liquidity_instance_report(iid))

SHADOW_ADAPTERS: dict[str, Callable[[], dict[str, Any]]] = {
    "CONTEXT_STRUCTURE_RETRACE_V1_PHASE7_OBSERVER": build_context_shadow_report,
}
SHADOW_BY_STRATEGY: dict[str, list[str]] = {
    "CONTEXT_STRUCTURE_RETRACE_V1": ["CONTEXT_STRUCTURE_RETRACE_V1_PHASE7_OBSERVER"],
}


def get_strategy_report(instance_id: str) -> tuple[bool, dict[str, Any]]:
    """Returns (found, data). Never raises for expected "no adapter" or
    "instance not live" cases — those are reported as a clean degraded body,
    not a 500. `found=False` with data["status"] distinguishes a known-but-
    not-live instance (NOT_LIVE) from an unrecognized identifier (NO_ADAPTER).
    """
    adapter = STRATEGY_ADAPTERS.get(instance_id)
    if adapter is None:
        return False, {"strategy_id": instance_id, "status": "NO_ADAPTER",
                        "message": "no observability adapter registered for this strategy/instance"}
    try:
        data = adapter()
    except Exception as exc:  # pragma: no cover - defensive; adapters are read-only
        return False, {"strategy_id": instance_id, "status": "ERROR",
                        "message": f"{type(exc).__name__}: {exc}"}
    if data is None:
        meta = LIQUIDITY_INSTANCES.get(instance_id)
        label = meta.get("label") if meta else instance_id
        return False, {"strategy_id": instance_id, "status": "NOT_LIVE",
                        "message": f"{label} is a defined variant with no running instance (no state file)"}
    return True, data


def get_strategy_shadows(strategy_id: str) -> list[dict[str, Any]]:
    """Always a list — empty when the strategy exists but has no shadow
    entity, never a 404 for that case."""
    results = []
    for shadow_id in SHADOW_BY_STRATEGY.get(strategy_id, []):
        adapter = SHADOW_ADAPTERS.get(shadow_id)
        if adapter is None:
            continue
        try:
            results.append(adapter())
        except Exception as exc:  # pragma: no cover - defensive; adapters are read-only
            results.append({"shadow_entity_id": shadow_id, "status": "ERROR",
                             "message": f"{type(exc).__name__}: {exc}"})
    return results


def list_family_instances(family_id: str) -> list[dict[str, Any]]:
    if family_id == "CONTEXT_STRUCTURE_RETRACE_V1":
        return [{"instance_id": "CONTEXT_STRUCTURE_RETRACE_V1", "label": "Context Structure Retrace V1",
                  "symbol": None, "live": True}]
    return [
        {"instance_id": meta["instance_id"], "label": meta["label"], "symbol": meta["symbol"],
         "live": _liquidity_instance_live(meta)}
        for meta in LIQUIDITY_INSTANCES.values() if meta["family_id"] == family_id
    ]
